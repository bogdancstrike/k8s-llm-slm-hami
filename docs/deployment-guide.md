# QSINT AI Platform PoC — Deployment Guide

## Prerequisites

### Cluster
- kubeadm Kubernetes cluster (v1.28+)
- Node with NVIDIA RTX 3080 (or any Ampere/Ada/Hopper GPU with 10GB+ vRAM)
- NVIDIA driver 535+ installed on the host
- NVIDIA Container Toolkit configured (default runtime = nvidia)
- Storage class supporting `ReadWriteMany` (NFS, Longhorn, CephFS) for model cache
- Storage class for `ReadWriteOnce` (local-path, hostpath) for stateful services

### Cluster components
Install BEFORE this PoC:
1. **ArgoCD** (`kubectl create namespace argocd && kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml`)
2. **cert-manager** (required by KServe): `helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set installCRDs=true`
3. **Prometheus + Grafana** via kube-prometheus-stack (for HAMi + LiteLLM metrics)

### Node preparation

```bash
# 1. Label the GPU node so HAMi device plugin lands on it
kubectl label node <your-gpu-node> gpu=true

# 2. Verify NVIDIA runtime is the default container runtime
ssh <gpu-node>
cat /etc/containerd/config.toml | grep -A 3 default_runtime_name
# Should show: default_runtime_name = "nvidia"

# 3. Verify GPU is visible to containerd
sudo ctr run --rm --runtime io.containerd.runc.v2 \
  --gpus 0 docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04 \
  test nvidia-smi
```

---

## Deployment steps

### Step 1: Fork & customize the monorepo

```bash
# Fork this repo to your GitHub
# Then update the repoURL in all 3 ArgoCD Application manifests:
# - platform/argocd/01-application-platform.yaml
# - platform/argocd/02-application-workloads.yaml
# - platform/argocd/03-application-kro-templates.yaml

sed -i 's|github.com/bogdancstrike/qsint-ai-platform|github.com/YOUR_USER/qsint-ai-platform|g' \
  platform/argocd/*.yaml
```

### Step 2: Pre-create secrets that ArgoCD shouldn't manage

```bash
# HuggingFace token (required for Gemma — gated model)
kubectl create namespace inference
kubectl create secret generic huggingface-token \
  -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE

# Change default passwords in production!
# Edit these files with stronger values before pushing:
#  - platform/postgresql/postgresql.yaml          (POSTGRES_PASSWORD)
#  - platform/litellm/litellm.yaml                (LITELLM_MASTER_KEY)
#  - platform/langfuse/langfuse.yaml              (NEXTAUTH_SECRET, ENCRYPTION_KEY)
#  - platform/open-webui/open-webui.yaml          (WEBUI_SECRET_KEY)
#  - platform/kserve/litellm-secret-mirror.yaml   (must match LITELLM_MASTER_KEY)
```

### Step 3: Bootstrap ArgoCD with the platform Applications

```bash
# Apply the ArgoCD Applications (this is the bootstrap step)
kubectl apply -k platform/argocd/

# Watch the sync progress
argocd app list
argocd app sync platform --prune
argocd app sync kro-templates --prune
argocd app sync workloads --prune
```

### Step 4: Verify HAMi is functioning

```bash
# Device plugin should be running on GPU node
kubectl -n hami-system get pods -l app.kubernetes.io/name=hami-device-plugin -o wide

# Node should advertise vGPU resources
kubectl describe node <your-gpu-node> | grep nvidia.com
# Expected:
#   nvidia.com/gpu: 10
#   nvidia.com/gpumem: 102400  (10 vGPUs * 10240MB)
#   nvidia.com/gpucores: 1000  (10 vGPUs * 100%)
```

### Step 5: Verify KServe is healthy

```bash
kubectl -n kserve get pods
# kserve-controller-manager-... should be Running

# Verify the vLLM ClusterServingRuntime exists
kubectl get clusterservingruntime vllm-runtime -o yaml
```

### Step 6: Verify KRO controller and RGD

```bash
# Controller running
kubectl -n kro-system get pods

# Our InferenceEndpoint RGD registered
kubectl get resourcegraphdefinitions
# Should show: inference-endpoint

# This should have created a CRD for InferenceEndpoint
kubectl get crd inferenceendpoints.kro.run
```

### Step 7: Check model pods come up

```bash
# Both InferenceEndpoint resources should exist
kubectl -n inference get inferenceendpoints
# NAME          STATUS
# gemma-1b      Ready
# smollm3-3b    Ready

# KServe InferenceServices created by KRO
kubectl -n inference get inferenceservices
# NAME          URL                                            READY
# gemma-1b      http://gemma-1b-predictor.inference.svc...     True
# smollm3-3b    http://smollm3-3b-predictor.inference.svc...   True

# Underlying pods should be scheduled on GPU node by HAMi
kubectl -n inference get pods -o wide

# Check that pods are on hami-scheduler
kubectl -n inference get pod <pod-name> -o jsonpath='{.spec.schedulerName}'
# Should print: hami-scheduler

# Watch vLLM logs to confirm model loaded
kubectl -n inference logs -f deploy/gemma-1b-predictor
# Look for: "Application startup complete"
```

### Step 8: Initialize Langfuse and wire to LiteLLM

```bash
# Open Langfuse UI
open http://<node-ip>:30030
# Sign up as the first user (becomes admin)
# Create a new project: qsint-poc
# Settings → API Keys → create a new pair
# Copy: pk-lf-... and sk-lf-...

# Patch litellm-secrets with Langfuse keys
kubectl -n ai-platform edit secret litellm-secrets
# Update LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (base64-encoded)

# Restart LiteLLM to pick up new secrets
kubectl -n ai-platform rollout restart deploy/litellm
```

### Step 9: Test end-to-end

```bash
# 1. Check models are registered in LiteLLM
LITELLM_URL=http://<node-ip>:<litellm-nodeport>   # or port-forward
kubectl -n ai-platform port-forward svc/litellm 4000:4000 &

curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer sk-litellm-master-change-me"
# Should show both gemma-1b-fast and smollm3-3b-quality

# 2. Send a chat completion to each model
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-litellm-master-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-1b-fast",
    "messages": [{"role": "user", "content": "Hello, who are you?"}],
    "max_tokens": 100
  }'

curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-litellm-master-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "smollm3-3b-quality",
    "messages": [{"role": "user", "content": "Explain Kubernetes in one sentence."}],
    "max_tokens": 100
  }'

# 3. Open the chat UI
open http://<node-ip>:30080
# Sign up, pick a model from the dropdown, chat

# 4. Verify traces appear in Langfuse
open http://<node-ip>:30030
# Project → Traces → should show your requests
```

---

## Verifying GPU sharing actually works

The whole point of HAMi is that **two model pods share one physical GPU**. Verify it:

```bash
# Both pods should be on the SAME node
kubectl -n inference get pods -l serving.kserve.io/inferenceservice -o wide

# Run nvidia-smi inside one of the pods — should show only its allocated vRAM
kubectl -n inference exec -it deploy/gemma-1b-predictor -- nvidia-smi
# vRAM Used should be < 5000 MiB (the HAMi limit)

# Run on the physical host (or via DCGM exporter metrics)
ssh <gpu-node>
nvidia-smi
# Should show TWO processes (vllm) on GPU 0, total memory ~10GB used
```

If you see only one process, check:
- Both pods are using `schedulerName: hami-scheduler` (check ServingRuntime applied correctly)
- HAMi scheduler logs: `kubectl -n hami-system logs -l app.kubernetes.io/name=hami-scheduler`

---

## Troubleshooting

### `OOM-killed` when loading model
- `gpu-memory-utilization` is too aggressive. Lower to 0.75 or 0.70.
- KV cache too large for context length. Reduce `maxModelLen` to 1024.

### vLLM hangs at startup
- HuggingFace download issue. Check pod has internet access.
- For Gemma: verify HF token has accepted the license on huggingface.co.

### Pod stuck Pending with `Insufficient nvidia.com/gpumem`
- HAMi accounting issue. Check `kubectl describe node <gpu-node>` — the allocatable vRAM should be ~10000 MB.
- If both pods request 5000 + 5000 = 10000 and node only has 10000, HAMi may reject due to overhead. Lower one pod to 4500.

### LiteLLM returns "model not found"
- KRO register Job failed. Check: `kubectl -n inference logs job/<name>-litellm-register`
- Re-run manually:
  ```bash
  kubectl -n inference delete job <name>-litellm-register
  kubectl -n inference annotate inferenceendpoint <name> kro.run/force-reconcile=true
  ```

### Langfuse callbacks not appearing
- Check LiteLLM pod env: `kubectl -n ai-platform exec deploy/litellm -- env | grep LANGFUSE`
- Restart LiteLLM after editing secret.
- Check Langfuse public/secret keys are correct (no spaces, valid base64 when stored).
