#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBECTL="${KUBECTL:-microk8s kubectl}"
HELM="${HELM:-microk8s helm3}"

wait_namespace_deleted() {
  local ns="$1"
  if $KUBECTL get namespace "$ns" >/dev/null 2>&1; then
    echo "Waiting for namespace $ns to terminate..."
    while $KUBECTL get namespace "$ns" >/dev/null 2>&1; do
      sleep 3
    done
  fi
}

helm_dep_update() {
  local chart="$1"
  echo "Updating dependencies for $chart"
  $HELM dependency update "$ROOT_DIR/$chart"
}

echo "Adding Helm repositories"
$HELM repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
$HELM repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
$HELM repo add hami-charts https://project-hami.github.io/HAMi/ >/dev/null 2>&1 || true
$HELM repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
$HELM repo update

echo "Removing existing platform-owned releases"
$HELM uninstall gitlab -n gitlab --wait --timeout 10m >/dev/null 2>&1 || true
$HELM uninstall kserve -n kserve --wait --timeout 5m >/dev/null 2>&1 || true
$HELM uninstall kserve-crd -n kserve --wait --timeout 5m >/dev/null 2>&1 || true
$HELM uninstall kro -n kro-system --wait --timeout 5m >/dev/null 2>&1 || true
$HELM uninstall hami -n kube-system --wait --timeout 5m >/dev/null 2>&1 || true
$HELM uninstall kube-prom-stack -n observability --wait --timeout 10m >/dev/null 2>&1 || true
$HELM uninstall argocd -n argocd --wait --timeout 5m >/dev/null 2>&1 || true
$HELM uninstall cert-manager -n cert-manager --wait --timeout 5m >/dev/null 2>&1 || true
$HELM uninstall qsint-namespaces -n default --wait --timeout 5m >/dev/null 2>&1 || true

echo "Deleting raw-install namespaces owned by the platform"
$KUBECTL delete namespace argocd cert-manager gitlab ai-platform inference kserve kro-system --ignore-not-found=true
wait_namespace_deleted argocd
wait_namespace_deleted cert-manager
wait_namespace_deleted gitlab
wait_namespace_deleted ai-platform
wait_namespace_deleted inference
wait_namespace_deleted kserve
wait_namespace_deleted kro-system

echo "Deleting legacy dashboard ConfigMaps that predate Helm ownership"
$KUBECTL delete configmap hami-native-dashboard -n observability --ignore-not-found=true
$KUBECTL delete servicemonitor \
  hami-scheduler-metrics \
  hami-device-plugin-metrics \
  -n observability \
  --ignore-not-found=true

echo "Deleting cert-manager CRDs from the previous raw install, if present"
$KUBECTL delete clusterrole \
  cert-manager-cainjector \
  cert-manager-cluster-view \
  cert-manager-controller-approve:cert-manager-io \
  cert-manager-controller-certificates \
  cert-manager-controller-certificatesigningrequests \
  cert-manager-controller-challenges \
  cert-manager-controller-clusterissuers \
  cert-manager-controller-ingress-shim \
  cert-manager-controller-issuers \
  cert-manager-controller-orders \
  cert-manager-edit \
  cert-manager-view \
  cert-manager-webhook:subjectaccessreviews \
  --ignore-not-found=true
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
  --ignore-not-found=true
$KUBECTL delete mutatingwebhookconfiguration cert-manager-webhook --ignore-not-found=true
$KUBECTL delete validatingwebhookconfiguration cert-manager-webhook --ignore-not-found=true
$KUBECTL delete role -n kube-system \
  cert-manager-cainjector:leaderelection \
  cert-manager:leaderelection \
  --ignore-not-found=true
$KUBECTL delete rolebinding -n kube-system \
  cert-manager-cainjector:leaderelection \
  cert-manager:leaderelection \
  --ignore-not-found=true
$KUBECTL delete crd \
  certificaterequests.cert-manager.io \
  certificates.cert-manager.io \
  challenges.acme.cert-manager.io \
  clusterissuers.cert-manager.io \
  issuers.cert-manager.io \
  orders.acme.cert-manager.io \
  --ignore-not-found=true

echo "Deleting Argo CD cluster-scoped objects from the previous raw install, if present"
$KUBECTL delete clusterrole \
  argocd-application-controller \
  argocd-applicationset-controller \
  argocd-server \
  --ignore-not-found=true
$KUBECTL delete clusterrolebinding \
  argocd-application-controller \
  argocd-applicationset-controller \
  argocd-server \
  --ignore-not-found=true
$KUBECTL delete crd \
  applications.argoproj.io \
  applicationsets.argoproj.io \
  appprojects.argoproj.io \
  --ignore-not-found=true

echo "Ensuring GPU node label expected by HAMi"
$KUBECTL label node bogdan gpu=on --overwrite

helm_dep_update charts/qsint-cert-manager
helm_dep_update charts/qsint-observability-stack
helm_dep_update charts/qsint-argocd
helm_dep_update charts/qsint-hami
helm_dep_update charts/qsint-kro
helm_dep_update charts/qsint-kserve-crd
helm_dep_update charts/qsint-kserve

echo "Installing cert-manager"
$HELM upgrade --install cert-manager "$ROOT_DIR/charts/qsint-cert-manager" \
  --namespace cert-manager --create-namespace --wait --timeout 10m

echo "Installing kube-prometheus-stack and Grafana"
$HELM upgrade --install kube-prom-stack "$ROOT_DIR/charts/qsint-observability-stack" \
  --namespace observability --create-namespace --wait --timeout 15m

echo "Installing Argo CD"
$HELM upgrade --install argocd "$ROOT_DIR/charts/qsint-argocd" \
  --namespace argocd --create-namespace --wait --timeout 10m

echo "Installing HAMi"
$HELM upgrade --install hami "$ROOT_DIR/charts/qsint-hami" \
  --namespace kube-system --wait --timeout 10m

echo "Installing KRO"
$HELM upgrade --install kro "$ROOT_DIR/charts/qsint-kro" \
  --namespace kro-system --create-namespace --wait --timeout 10m

echo "Installing KServe CRDs"
$HELM upgrade --install kserve-crd "$ROOT_DIR/charts/qsint-kserve-crd" \
  --namespace kserve --create-namespace --wait --timeout 10m

echo "Installing KServe"
$HELM upgrade --install kserve "$ROOT_DIR/charts/qsint-kserve" \
  --namespace kserve --wait --timeout 10m

echo "Installing GitLab"
$HELM upgrade --install gitlab "$ROOT_DIR/charts/qsint-gitlab" \
  --namespace gitlab --create-namespace \
  --values "$ROOT_DIR/charts/qsint-gitlab/qsint-values.yaml" \
  --wait --timeout 30m

echo "Installing QSINT namespaces"
$HELM upgrade --install qsint-namespaces "$ROOT_DIR/charts/qsint-namespaces" \
  --namespace default --wait --timeout 5m

echo "Installing QSINT platform manifests"
$HELM upgrade --install qsint-platform "$ROOT_DIR/charts/qsint-platform" \
  --namespace ai-platform --wait --timeout 15m

echo "Installing KRO templates"
$HELM upgrade --install qsint-kro-templates "$ROOT_DIR/charts/qsint-kro-templates" \
  --namespace kro-system --wait --timeout 10m

echo "Waiting for InferenceEndpoint CRD"
$KUBECTL wait --for=condition=Established crd/inferenceendpoints.kro.run --timeout=180s

echo "Installing example workloads"
$HELM upgrade --install qsint-workloads "$ROOT_DIR/charts/qsint-workloads" \
  --namespace inference --wait --timeout 10m

echo "Deployment complete"
$KUBECTL get ingress -A
