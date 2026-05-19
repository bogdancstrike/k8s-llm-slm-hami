#!/usr/bin/env bash
# deploy-microk8s.sh — fresh teardown + redeploy of the QSINT AI Platform PoC.
#
# Scope: only the PoC stack we own (charts/qsint-*). Leaves pre-existing,
# unrelated releases in the cluster alone (e.g. nvidia-device-plugin,
# gpu-operator, loki, tempo). Use `WIPE_EXTRA="loki tempo"` to also remove
# specific extra releases by name.
#
# Usage:
#   ./scripts/deploy-microk8s.sh                # interactive, with confirmation
#   ./scripts/deploy-microk8s.sh --yes          # skip the confirmation prompt
#   CONFIRM=yes ./scripts/deploy-microk8s.sh    # equivalent
#   SKIP_TEARDOWN=1 ./scripts/deploy-microk8s.sh   # only install/upgrade
#   HUGGINGFACE_TOKEN=hf_xxx ./scripts/deploy-microk8s.sh
#
# Env:
#   KUBECTL              kubectl command       (default: `microk8s kubectl`)
#   HELM                 helm command          (default: `microk8s helm3`)
#   GPU_NODE             node to label gpu=on  (default: `bogdan`)
#   HUGGINGFACE_TOKEN    optional HF API token; if set, creates
#                        secret/huggingface-token in `inference`.
#   WIPE_EXTRA           extra releases to uninstall, space-separated.
#   SKIP_TEARDOWN        if set, skip the destructive phase.
#   CONFIRM              "yes" to skip the interactive confirmation.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL="${KUBECTL:-microk8s kubectl}"
HELM="${HELM:-microk8s helm3}"
GPU_NODE="${GPU_NODE:-bogdan}"
WIPE_EXTRA="${WIPE_EXTRA:-}"

# ─── helpers ────────────────────────────────────────────────────────────────

log()  { echo -e "\033[1;36m>>> $*\033[0m"; }
warn() { echo -e "\033[1;33m!!! $*\033[0m" >&2; }
die()  { echo -e "\033[1;31mXXX $*\033[0m" >&2; exit 1; }

confirm() {
  if [[ "${1:-}" == "--yes" || "${CONFIRM:-}" == "yes" ]]; then
    return 0
  fi
  cat <<'EOF'

This will UNINSTALL every PoC component in the cluster and reinstall it.
You will lose:
  - All running model pods (weights re-download from HF on next boot)
  - All Langfuse projects, users and API keys
  - All LiteLLM model registrations (re-created by registration Jobs)
  - All Jaeger traces (in-memory backend)
  - Argo CD / GitLab / Grafana initial credentials (rotated on install)
  - The model-cache PVC (50 Gi of GGUF + HF weight downloads)

Pre-existing releases NOT touched: gpu-operator, nvidia-device-plugin, loki, tempo.

Type YES (uppercase) to proceed, anything else to abort:
EOF
  read -r ans
  [[ "$ans" == "YES" ]] || die "aborted by user"
}

wait_namespace_deleted() {
  local ns="$1" timeout="${2:-300}" elapsed=0
  if ! $KUBECTL get namespace "$ns" >/dev/null 2>&1; then
    return 0
  fi
  log "waiting for namespace '$ns' to terminate (timeout ${timeout}s)"
  while $KUBECTL get namespace "$ns" >/dev/null 2>&1; do
    if (( elapsed >= timeout )); then
      warn "namespace '$ns' still present after ${timeout}s — clearing finalizers"
      force_clear_namespace "$ns"
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
}

force_clear_namespace() {
  # Last-resort: strip the namespace's metadata.finalizers via the raw API
  # (works only because microk8s exposes kube-apiserver on localhost).
  local ns="$1"
  $KUBECTL get namespace "$ns" -o json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); d["spec"].pop("finalizers",None); print(json.dumps(d))' \
    | $KUBECTL replace --raw "/api/v1/namespaces/$ns/finalize" -f - >/dev/null \
    || warn "failed to force-clear $ns finalizers (may already be gone)"
}

clear_finalizers_for() {
  # Remove metadata.finalizers from every object of a given kind in a namespace.
  # Used to unstick KServe / KRO CRs before their controllers are torn down.
  local kind="$1" ns="$2"
  local names
  names=$($KUBECTL -n "$ns" get "$kind" -o name 2>/dev/null || true)
  [[ -z "$names" ]] && return 0
  while IFS= read -r obj; do
    $KUBECTL -n "$ns" patch "$obj" --type merge -p '{"metadata":{"finalizers":null}}' \
      >/dev/null 2>&1 || true
  done <<< "$names"
}

helm_dep_update() {
  log "helm dependency update $1"
  $HELM dependency update "$ROOT_DIR/$1"
}

uninstall_release() {
  local rel="$1" ns="$2" timeout="${3:-10m}"
  if $HELM list -n "$ns" -q 2>/dev/null | grep -qx "$rel"; then
    log "uninstalling helm release '$rel' (ns=$ns)"
    $HELM uninstall "$rel" -n "$ns" --wait --timeout "$timeout" >/dev/null 2>&1 || \
      warn "helm uninstall $rel failed (continuing)"
  fi
}

# ─── confirmation ──────────────────────────────────────────────────────────

confirm "${1:-}"

log "starting QSINT AI Platform fresh deploy"
log "root:    $ROOT_DIR"
log "kubectl: $KUBECTL"
log "helm:    $HELM"
log "gpu node: $GPU_NODE"

# ─── pre-flight ────────────────────────────────────────────────────────────

log "verifying cluster reachable"
$KUBECTL get nodes >/dev/null || die "cluster not reachable via '$KUBECTL'"

log "verifying GPU node '$GPU_NODE' exists"
$KUBECTL get node "$GPU_NODE" >/dev/null || die "node '$GPU_NODE' not found"

# ─── teardown ──────────────────────────────────────────────────────────────

if [[ -z "${SKIP_TEARDOWN:-}" ]]; then
  log "phase 1/3 — TEARDOWN"

  # Unstick KRO + KServe CRs before their controllers go away — otherwise the
  # namespace deletion below will hang on finalizers we can no longer clear.
  for ns in inference; do
    if $KUBECTL get namespace "$ns" >/dev/null 2>&1; then
      log "clearing KRO/KServe finalizers in $ns"
      clear_finalizers_for inferenceendpoints.kro.run "$ns"
      clear_finalizers_for inferenceservices.serving.kserve.io "$ns"
      $KUBECTL -n "$ns" delete inferenceendpoints.kro.run --all --ignore-not-found=true --timeout=60s || true
      $KUBECTL -n "$ns" delete inferenceservices.serving.kserve.io --all --ignore-not-found=true --timeout=60s || true
    fi
  done

  # Uninstall in REVERSE dependency order so consumers go before their CRDs.
  uninstall_release qsint-workloads      inference   5m
  uninstall_release qsint-kro-templates  kro-system  5m
  uninstall_release qsint-platform       ai-platform 10m
  uninstall_release qsint-namespaces     default     5m
  uninstall_release gitlab               gitlab      15m
  uninstall_release kserve               kserve      10m
  uninstall_release kserve-crd           kserve      5m
  uninstall_release kro                  kro-system  5m
  uninstall_release hami                 kube-system 5m
  uninstall_release argocd               argocd      5m
  uninstall_release kube-prom-stack      observability 10m
  uninstall_release cert-manager         cert-manager 5m

  for rel in $WIPE_EXTRA; do
    log "wiping extra release: $rel"
    ns=$($HELM list -A -q -f "^${rel}$" -o json 2>/dev/null \
         | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["namespace"] if d else "")' 2>/dev/null || true)
    [[ -n "$ns" ]] && uninstall_release "$rel" "$ns" 10m
  done

  log "deleting PoC namespaces (cascades remaining objects)"
  $KUBECTL delete namespace argocd cert-manager gitlab ai-platform inference \
    kserve kro-system --ignore-not-found=true --wait=false || true

  wait_namespace_deleted argocd       300
  wait_namespace_deleted cert-manager 300
  wait_namespace_deleted gitlab       600
  wait_namespace_deleted ai-platform  300
  wait_namespace_deleted inference    300
  wait_namespace_deleted kserve       300
  wait_namespace_deleted kro-system   300

  log "deleting legacy CRDs from any prior raw install"
  $KUBECTL delete crd \
    inferenceendpoints.kro.run \
    resourcegraphdefinitions.kro.run \
    instances.kro.run \
    inferenceservices.serving.kserve.io \
    inferencegraphs.serving.kserve.io \
    predictors.serving.kserve.io \
    servingruntimes.serving.kserve.io \
    clusterservingruntimes.serving.kserve.io \
    clusterstoragecontainers.serving.kserve.io \
    trainedmodels.serving.kserve.io \
    localmodelcaches.serving.kserve.io \
    localmodelnodes.serving.kserve.io \
    localmodelnodegroups.serving.kserve.io \
    certificaterequests.cert-manager.io \
    certificates.cert-manager.io \
    challenges.acme.cert-manager.io \
    clusterissuers.cert-manager.io \
    issuers.cert-manager.io \
    orders.acme.cert-manager.io \
    applications.argoproj.io \
    applicationsets.argoproj.io \
    appprojects.argoproj.io \
    --ignore-not-found=true --wait=false || true

  log "deleting orphaned cluster-scoped RBAC from any prior raw install"
  $KUBECTL delete mutatingwebhookconfiguration cert-manager-webhook \
    --ignore-not-found=true || true
  $KUBECTL delete validatingwebhookconfiguration cert-manager-webhook \
    --ignore-not-found=true || true
  # Leftover ClusterRoles/Bindings whose names predate Helm ownership labels
  $KUBECTL delete clusterrole \
    cert-manager-cainjector cert-manager-cluster-view \
    cert-manager-controller-approve:cert-manager-io \
    cert-manager-controller-certificates \
    cert-manager-controller-certificatesigningrequests \
    cert-manager-controller-challenges \
    cert-manager-controller-clusterissuers \
    cert-manager-controller-ingress-shim \
    cert-manager-controller-issuers \
    cert-manager-controller-orders \
    cert-manager-edit cert-manager-view \
    cert-manager-webhook:subjectaccessreviews \
    argocd-application-controller \
    argocd-applicationset-controller \
    argocd-server \
    --ignore-not-found=true || true
  $KUBECTL delete clusterrolebinding \
    cert-manager-cainjector \
    cert-manager-controller-approve:cert-manager-io \
    cert-manager-controller-certificates \
    cert-manager-controller-certificatesigningrequests \
    cert-manager-controller-challenges \
    cert-manager-controller-clusterissuers \
    cert-manager-controller-ingress-shim \
    cert-manager-controller-issuers \
    cert-manager-controller-orders \
    cert-manager-webhook:subjectaccessreviews \
    argocd-application-controller \
    argocd-applicationset-controller \
    argocd-server \
    --ignore-not-found=true || true
  $KUBECTL delete role -n kube-system \
    cert-manager-cainjector:leaderelection \
    cert-manager:leaderelection \
    --ignore-not-found=true || true
  $KUBECTL delete rolebinding -n kube-system \
    cert-manager-cainjector:leaderelection \
    cert-manager:leaderelection \
    --ignore-not-found=true || true

  # Legacy ConfigMaps + ServiceMonitors in `observability` that predate Helm.
  $KUBECTL -n observability delete configmap hami-native-dashboard \
    --ignore-not-found=true || true
  $KUBECTL -n observability delete servicemonitor \
    hami-scheduler-metrics hami-device-plugin-metrics \
    --ignore-not-found=true || true
else
  log "SKIP_TEARDOWN set — skipping destructive phase"
fi

# ─── install ───────────────────────────────────────────────────────────────

log "phase 2/3 — INSTALL"

log "adding helm repos"
$HELM repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
$HELM repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
$HELM repo add hami-charts https://project-hami.github.io/HAMi/ >/dev/null 2>&1 || true
$HELM repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
$HELM repo update >/dev/null

log "labeling GPU node ($GPU_NODE) with gpu=on"
$KUBECTL label node "$GPU_NODE" gpu=on --overwrite

for chart in qsint-cert-manager qsint-observability-stack qsint-argocd \
             qsint-hami qsint-kro qsint-kserve-crd qsint-kserve; do
  helm_dep_update "charts/$chart"
done

log "installing cert-manager"
$HELM upgrade --install cert-manager "$ROOT_DIR/charts/qsint-cert-manager" \
  --namespace cert-manager --create-namespace --wait --timeout 10m

log "installing kube-prometheus-stack (Prometheus + Grafana + Alertmanager)"
$HELM upgrade --install kube-prom-stack "$ROOT_DIR/charts/qsint-observability-stack" \
  --namespace observability --create-namespace --wait --timeout 15m

log "installing Argo CD"
$HELM upgrade --install argocd "$ROOT_DIR/charts/qsint-argocd" \
  --namespace argocd --create-namespace --wait --timeout 10m

log "installing HAMi vGPU scheduler"
$HELM upgrade --install hami "$ROOT_DIR/charts/qsint-hami" \
  --namespace kube-system --wait --timeout 10m

log "installing KRO controller"
$HELM upgrade --install kro "$ROOT_DIR/charts/qsint-kro" \
  --namespace kro-system --create-namespace --wait --timeout 10m

log "installing KServe CRDs"
$HELM upgrade --install kserve-crd "$ROOT_DIR/charts/qsint-kserve-crd" \
  --namespace kserve --create-namespace --wait --timeout 10m

log "installing KServe controller"
$HELM upgrade --install kserve "$ROOT_DIR/charts/qsint-kserve" \
  --namespace kserve --wait --timeout 10m

log "installing GitLab CE (slim values)"
$HELM upgrade --install gitlab "$ROOT_DIR/charts/qsint-gitlab" \
  --namespace gitlab --create-namespace \
  --values "$ROOT_DIR/charts/qsint-gitlab/qsint-values.yaml" \
  --wait --timeout 30m

log "installing QSINT namespaces (ai-platform, inference)"
$HELM upgrade --install qsint-namespaces "$ROOT_DIR/charts/qsint-namespaces" \
  --namespace default --wait --timeout 5m

if [[ -n "${HUGGINGFACE_TOKEN:-}" ]]; then
  log "creating huggingface-token secret in 'inference' from \$HUGGINGFACE_TOKEN"
  $KUBECTL create secret generic huggingface-token -n inference \
    --from-literal=token="$HUGGINGFACE_TOKEN" --dry-run=client -o yaml \
    | $KUBECTL apply -f -
fi

log "installing QSINT platform (LiteLLM, Open WebUI, Langfuse, Postgres, OTel, Jaeger, runtimes)"
$HELM upgrade --install qsint-platform "$ROOT_DIR/charts/qsint-platform" \
  --namespace ai-platform --wait --timeout 15m

log "installing KRO templates (InferenceEndpoint RGD)"
$HELM upgrade --install qsint-kro-templates "$ROOT_DIR/charts/qsint-kro-templates" \
  --namespace kro-system --wait --timeout 10m

log "waiting for InferenceEndpoint CRD to be Established"
$KUBECTL wait --for=condition=Established crd/inferenceendpoints.kro.run --timeout=180s

log "installing example workloads (3 InferenceEndpoints + LiteLLM register jobs)"
$HELM upgrade --install qsint-workloads "$ROOT_DIR/charts/qsint-workloads" \
  --namespace inference --wait --timeout 10m

# ─── post-install ──────────────────────────────────────────────────────────

log "phase 3/3 — POST-INSTALL"
log "ingresses:"
$KUBECTL get ingress -A

log "all model pods:"
$KUBECTL -n inference get pods -l serving.kserve.io/inferenceservice -o wide || true

cat <<'EOF'

==============================================================================
  Deploy complete.

  Map *.local.ro hostnames to 127.0.0.1 once on your workstation:
    sudo ./scripts/update-local-hosts.sh

  Initial credentials:
    Argo CD admin    : kubectl -n argocd get secret argocd-initial-admin-secret \
                         -o go-template='{{index .data "password" | base64decode}}{{"\n"}}'
    GitLab root      : kubectl -n gitlab get secret gitlab-initial-root-password \
                         -o go-template='{{index .data "password" | base64decode}}{{"\n"}}'
    Grafana admin    : kubectl -n observability get secret kube-prom-stack-grafana \
                         -o go-template='{{index .data "admin-password" | base64decode}}{{"\n"}}'
    LiteLLM bearer   : kubectl -n ai-platform get secret litellm-secrets \
                         -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}{{"\n"}}'

  Smoke test:
    LITELLM_KEY=$(microk8s kubectl -n ai-platform get secret litellm-secrets \
                    -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}') \
      python3 tests/e2e_smoke.py
==============================================================================
EOF
