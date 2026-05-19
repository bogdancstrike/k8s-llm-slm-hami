#!/usr/bin/env bash
# Bootstrap script — one-time setup before ArgoCD takes over.
#
# Prerequisites:
#   - kubectl configured for target cluster
#   - GPU node already exists with NVIDIA driver installed
#   - ArgoCD, cert-manager, prometheus-operator already installed
#
# Usage:
#   ./bootstrap.sh <gpu-node-name> <huggingface-token>

set -euo pipefail

GPU_NODE="${1:-}"
HF_TOKEN="${2:-}"

if [ -z "$GPU_NODE" ]; then
  echo "Usage: $0 <gpu-node-name> [huggingface-token]"
  echo ""
  echo "Available nodes:"
  kubectl get nodes -o name
  exit 1
fi

echo ">>> Labeling GPU node: $GPU_NODE"
kubectl label node "$GPU_NODE" gpu=true --overwrite

echo ">>> Verifying NVIDIA GPU is visible on the node"
kubectl describe node "$GPU_NODE" | grep -E "nvidia.com/gpu" || {
  echo "WARNING: nvidia.com/gpu not visible on node. Install NVIDIA device plugin or wait for HAMi sync."
}

echo ">>> Creating inference namespace (so we can create secrets in it)"
kubectl create namespace inference --dry-run=client -o yaml | kubectl apply -f -

echo ">>> Creating HuggingFace token secret"
if [ -n "$HF_TOKEN" ]; then
  kubectl create secret generic huggingface-token \
    -n inference \
    --from-literal=token="$HF_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "    Token configured"
else
  echo "    No token provided — creating empty secret (Gemma will fail to download)"
  kubectl create secret generic huggingface-token \
    -n inference \
    --from-literal=token="" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

echo ""
echo ">>> Bootstrap complete. Now apply the ArgoCD applications:"
echo ""
echo "    kubectl apply -k platform/argocd/"
echo ""
echo "Then watch the sync:"
echo "    argocd app list"
echo "    argocd app sync platform"
echo "    argocd app sync kro-templates"
echo "    argocd app sync workloads"
echo ""
echo "After Langfuse is up, follow docs/deployment-guide.md Step 8 to wire keys."
