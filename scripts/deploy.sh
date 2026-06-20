#!/usr/bin/env bash
# deploy.sh — fresh teardown + redeploy of the AI Platform PoC.
#
# Scope: only the PoC stack we own (the charts under charts/, one Helm release
# per app). Leaves pre-existing,
# unrelated releases in the cluster alone (e.g. nvidia-device-plugin,
# gpu-operator, loki, tempo). Use `WIPE_EXTRA="loki tempo"` to also remove
# specific extra releases by name.
#
# Usage:
#   ./scripts/deploy.sh                # MicroK8s (default), interactive
#   ./scripts/deploy.sh --microk8s     # MicroK8s explicitly
#   ./scripts/deploy.sh --k8s          # classic Kubernetes (kubectl + helm)
#   ./scripts/deploy.sh --k8s --yes    # classic k8s, skip confirmation
#   CONFIRM=yes ./scripts/deploy.sh    # equivalent to --yes
#   SKIP_TEARDOWN=1 ./scripts/deploy.sh   # only install/upgrade
#   HUGGINGFACE_TOKEN=hf_xxx ./scripts/deploy.sh
#
# Mode (--microk8s | --k8s) selects the default kubectl/helm commands:
#   --microk8s : KUBECTL="microk8s kubectl", HELM="microk8s helm3"  (default)
#   --k8s      : KUBECTL="kubectl",          HELM="helm"
# Explicit KUBECTL=/HELM= env vars always override the mode defaults.
#
# Env:
#   KUBECTL              kubectl command       (overrides the mode default)
#   HELM                 helm command          (overrides the mode default)
#   GPU_NODE             node to label gpu=on  (default: `bogdan`)
#   HUGGINGFACE_TOKEN    optional HF API token; if set, creates
#                        secret/huggingface-token in `inference`.
#   WIPE_EXTRA           extra releases to uninstall, space-separated.
#   SKIP_TEARDOWN        if set, skip the destructive phase.
#   CONFIRM              "yes" to skip the interactive confirmation.

set -euo pipefail

# ─── argument parsing (mode + flags, any order) ──────────────────────────────
MODE="microk8s"
CONFIRM_ARG=""
for arg in "$@"; do
  case "$arg" in
    --microk8s)      MODE="microk8s" ;;
    --k8s|--kubectl) MODE="k8s" ;;
    --yes|-y)        CONFIRM_ARG="--yes" ;;
    -h|--help)       sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (use --microk8s | --k8s | --yes)" >&2; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$MODE" == "k8s" ]]; then
  KUBECTL="${KUBECTL:-kubectl}"
  HELM="${HELM:-helm}"
else
  KUBECTL="${KUBECTL:-microk8s kubectl}"
  HELM="${HELM:-microk8s helm3}"
fi
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
  - Grafana initial credentials (rotated on install)
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

secret_val() {
  # Decode a single key from a Secret. Prints the value, or "<unavailable>"
  # if the secret/key is missing. Used only for the post-install summary.
  local ns="$1" name="$2" key="$3" val
  val=$($KUBECTL -n "$ns" get secret "$name" \
          -o go-template="{{index .data \"$key\" | base64decode}}" 2>/dev/null) || true
  [[ -n "$val" ]] && echo "$val" || echo "<unavailable>"
}

# ─── confirmation ──────────────────────────────────────────────────────────

confirm "$CONFIRM_ARG"

log "starting AI Platform fresh deploy"
log "mode:    $MODE"
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
  uninstall_release ai-models            inference   5m
  uninstall_release kro-templates  kro-system  5m
  uninstall_release serving-runtimes     inference   5m
  uninstall_release monitoring           observability 5m
  uninstall_release open-webui           ai-platform 5m
  uninstall_release jaeger               ai-platform 5m
  uninstall_release otel-collector       ai-platform 5m
  uninstall_release litellm              ai-platform 5m
  uninstall_release langfuse             ai-platform 5m
  uninstall_release postgresql           ai-platform 10m
  uninstall_release namespaces     default     5m
  uninstall_release kserve               kserve      10m
  uninstall_release kserve-crd           kserve      5m
  uninstall_release kro                  kro-system  5m
  uninstall_release hami                 kube-system 5m
  uninstall_release kube-prom-stack      observability 10m
  uninstall_release cert-manager         cert-manager 5m

  for rel in $WIPE_EXTRA; do
    log "wiping extra release: $rel"
    ns=$($HELM list -A -q -f "^${rel}$" -o json 2>/dev/null \
         | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["namespace"] if d else "")' 2>/dev/null || true)
    [[ -n "$ns" ]] && uninstall_release "$rel" "$ns" 10m
  done

  log "deleting PoC namespaces (cascades remaining objects)"
  $KUBECTL delete namespace cert-manager ai-platform inference \
    kserve kro-system --ignore-not-found=true --wait=false || true

  wait_namespace_deleted cert-manager 300
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
$HELM repo update >/dev/null

log "labeling GPU node ($GPU_NODE) with gpu=on"
$KUBECTL label node "$GPU_NODE" gpu=on --overwrite

for chart in cert-manager observability-stack \
             hami kro kserve-crd kserve; do
  helm_dep_update "charts/$chart"
done

log "installing cert-manager"
$HELM upgrade --install cert-manager "$ROOT_DIR/charts/cert-manager" \
  --namespace cert-manager --create-namespace --wait --timeout 10m

log "installing kube-prometheus-stack (Prometheus + Grafana + Alertmanager)"
$HELM upgrade --install kube-prom-stack "$ROOT_DIR/charts/observability-stack" \
  --namespace observability --create-namespace --wait --timeout 15m

log "installing HAMi vGPU scheduler"
$HELM upgrade --install hami "$ROOT_DIR/charts/hami" \
  --namespace kube-system --wait --timeout 10m

log "installing KRO controller"
# The KRO CRDs live in the chart's crds/ dir, which Helm only applies when the
# CRD is ABSENT. Teardown deletes them with --wait=false, so a fresh `kro`
# install can race a still-Terminating CRD and silently skip it. Wait for the
# CRDs to be fully gone first so the install recreates them cleanly.
for crd in resourcegraphdefinitions.kro.run instances.kro.run; do
  $KUBECTL wait --for=delete "crd/$crd" --timeout=120s >/dev/null 2>&1 || true
done
$HELM upgrade --install kro "$ROOT_DIR/charts/kro" \
  --namespace kro-system --create-namespace --wait --timeout 10m
log "verifying KRO CRDs are established"
if ! $KUBECTL wait --for=condition=Established crd/resourcegraphdefinitions.kro.run --timeout=60s >/dev/null 2>&1; then
  warn "KRO CRDs missing after install — applying them from the chart"
  $HELM template kro "$ROOT_DIR/charts/kro" --include-crds --namespace kro-system \
    | $KUBECTL apply -n kro-system -f - >/dev/null
  $KUBECTL wait --for=condition=Established crd/resourcegraphdefinitions.kro.run --timeout=120s
fi

log "installing KServe CRDs"
$HELM upgrade --install kserve-crd "$ROOT_DIR/charts/kserve-crd" \
  --namespace kserve --create-namespace --wait --timeout 10m

log "installing KServe controller"
$HELM upgrade --install kserve "$ROOT_DIR/charts/kserve" \
  --namespace kserve --wait --timeout 10m

log "installing namespaces (ai-platform, inference)"
$HELM upgrade --install namespaces "$ROOT_DIR/charts/namespaces" \
  --namespace default --wait --timeout 5m

# ── Platform apps (each app is now its own chart) ───────────────────────────
# Postgres first — LiteLLM (and Open WebUI) block on it. Langfuse runs its own
# bundled DB stack (Postgres + ClickHouse + Redis + S3) via its upstream chart.
log "installing PostgreSQL (shared LiteLLM + Open WebUI DB)"
$HELM upgrade --install postgresql "$ROOT_DIR/charts/postgresql" \
  --namespace ai-platform --wait --timeout 10m

log "installing LiteLLM (OpenAI-compatible gateway)"
$HELM upgrade --install litellm "$ROOT_DIR/charts/litellm" \
  --namespace ai-platform --wait --timeout 10m

# Langfuse v3 (upstream chart) brings ClickHouse/Redis/MinIO — heavier, so a
# longer timeout, and non-fatal if it lags (the rest of the stack is usable).
log "installing Langfuse (LLM tracing UI + bundled DB stack)"
$HELM upgrade --install langfuse "$ROOT_DIR/charts/langfuse" \
  --namespace ai-platform --wait --timeout 20m \
  || warn "langfuse not fully ready yet (continuing) — check 'kubectl -n ai-platform get pods'"

log "installing OpenTelemetry Collector"
$HELM upgrade --install otel-collector "$ROOT_DIR/charts/otel-collector" \
  --namespace ai-platform --wait --timeout 10m

log "installing Jaeger (tracing backend + UI)"
$HELM upgrade --install jaeger "$ROOT_DIR/charts/jaeger" \
  --namespace ai-platform --wait --timeout 10m

log "installing Open WebUI (chat interface, Postgres-backed)"
$HELM upgrade --install open-webui "$ROOT_DIR/charts/open-webui" \
  --namespace ai-platform --wait --timeout 10m

log "installing KServe serving runtimes (vLLM GPU + llama.cpp CPU)"
$HELM upgrade --install serving-runtimes "$ROOT_DIR/charts/serving-runtimes" \
  --namespace inference --wait --timeout 10m

if [[ -n "${HUGGINGFACE_TOKEN:-}" ]]; then
  log "patching huggingface-token secret in 'inference' from \$HUGGINGFACE_TOKEN"
  $KUBECTL -n inference patch secret huggingface-token --type merge \
    -p "{\"stringData\":{\"token\":\"$HUGGINGFACE_TOKEN\"}}"
fi

log "installing monitoring (Grafana dashboards + HAMi ServiceMonitors)"
$HELM upgrade --install monitoring "$ROOT_DIR/charts/monitoring" \
  --namespace observability --wait --timeout 10m

log "installing KRO templates (InferenceEndpoint RGD)"
$HELM upgrade --install kro-templates "$ROOT_DIR/charts/kro-templates" \
  --namespace kro-system --wait --timeout 10m

log "waiting for InferenceEndpoint CRD to be Established"
$KUBECTL wait --for=condition=Established crd/inferenceendpoints.kro.run --timeout=180s

log "installing example models (3 InferenceEndpoints + LiteLLM register jobs)"
$HELM upgrade --install ai-models "$ROOT_DIR/charts/ai-models" \
  --namespace inference --wait --timeout 10m

# ─── post-install ──────────────────────────────────────────────────────────

log "phase 3/3 — POST-INSTALL"
log "ingresses:"
$KUBECTL get ingress -A

log "all model pods:"
$KUBECTL -n inference get pods -l serving.kserve.io/inferenceservice -o wide || true

# ─── deploy summary ──────────────────────────────────────────────────────────
# Live credentials pulled from the cluster so rotated/changed values are correct.

GRAFANA_PW=$(secret_val observability kube-prom-stack-grafana admin-password)
LITELLM_KEY=$(secret_val ai-platform litellm-secrets LITELLM_MASTER_KEY)

cat <<EOF

==============================================================================
  Deploy complete — AI Platform PoC

  Components deployed (one Helm release per app):
    cert-manager, kube-prometheus-stack (Prometheus/Grafana/Alertmanager),
    HAMi vGPU scheduler, KRO, KServe (+CRDs), postgresql, litellm, langfuse,
    otel-collector, jaeger, open-webui, serving-runtimes, monitoring,
    KRO templates, and ai-models (3 example InferenceEndpoints).

  Map *.local.ro hostnames to 127.0.0.1 once on your workstation:
    sudo ./scripts/update-local-hosts.sh

  ----------------------------------------------------------------------------
  Service       Access URL                  Login
  ----------------------------------------------------------------------------
  Open WebUI    http://open-webui.local.ro  create admin on first visit (signup)
  Langfuse      http://langfuse.local.ro    sign up on first visit; default
                                            project: ai-platform
  LiteLLM       http://litellm.local.ro/ui  user: admin
                                            password: ${LITELLM_KEY}
  Grafana       http://grafana.local.ro     user: admin
                                            password: ${GRAFANA_PW}
  Jaeger        http://jaeger.local.ro      no authentication
  ----------------------------------------------------------------------------

  API access:
    LiteLLM bearer token : ${LITELLM_KEY}

  Smoke test:
    LITELLM_KEY='${LITELLM_KEY}' python3 tests/e2e_smoke.py
==============================================================================
EOF
