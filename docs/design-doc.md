# Design Doc — QSINT AI Platform PoC

**Status:** PoC (Proof of Concept)
**Date:** 2026-05-18
**Author:** Bogdan
**Inspired by:** AWS re:Invent 2026 KRO-based AI Platform talk

---

## 1. Context

The AWS talk demonstrates a Kubernetes-native AI platform where:
- KRO abstracts model deployment into a single `InferenceEndpoint` CRD
- ArgoCD provides GitOps-driven sync
- Ray Serve + vLLM handle model inference
- LiteLLM provides an OpenAI-compatible gateway
- Open WebUI offers a chat interface
- Langfuse tracks LLM observability

The AWS version uses **EKS + Karpenter + ACK + Knative-backed KServe**. None of that
applies to a homelab kubeadm cluster with one RTX 3080.

**Goal of this PoC:** Reproduce the same developer experience (`git push` → `git push`
→ model live) using only on-prem-friendly components, demonstrating GPU sharing through
HAMi.

---

## 2. Key design decisions

### 2.1 KServe over Ray Serve

The AWS slides use Ray Serve via KubeRay. Why we chose KServe instead:

| Criterion | KServe | Ray Serve |
|---|---|---|
| Multi-model serving | 1 IS per model (clean isolation) | 1 RayService can host multiple, but couples them |
| Single-GPU fit | Natural — single pod per model | Overkill — distributed inference unused |
| OpenAI API surface | Via vLLM ServingRuntime | Via vLLM ServingRuntime |
| Autoscaling | HPA + KServe controller | KubeRay + Ray autoscaler |
| Operator surface area | Smaller — one CRD (InferenceService) | Larger — RayCluster + RayService + RayJob |
| Standard runtime spec | ServingRuntime / ClusterServingRuntime | Custom RayService config |

For our setup (1 GPU, 2 models, no need for distributed inference), KServe is a cleaner
fit. Ray's value is tensor parallelism across many GPUs — irrelevant here.

**Honest critique:** If we later scale to multi-node H200 GPUs (per your QSINT LLM
architecture work) and need TP=4, Ray Serve becomes valuable. The decision should
be re-evaluated at that scale. KServe does support `workers > 1` for distributed
serving, but Ray's RadixAttention KV-cache reuse is genuinely useful for RAG.

### 2.2 RawDeployment mode (no Knative)

KServe has two deployment modes:
- **Serverless** (default) — backed by Knative, with scale-to-zero
- **RawDeployment** — plain K8s Deployment + HPA

We pick RawDeployment because:
- Knative adds a controller, queue-proxy sidecar, and Istio dependency
- vLLM cold-start is 60-300s — scale-to-zero is impractical anyway
- For 2 models always running, the savings from Knative are zero

### 2.3 vLLM downloads from HuggingFace at runtime (no model registry pre-fetch)

The AWS slide shows a model registry box leading to the deployment. We skip this:
- For PoC, simpler to let vLLM call HuggingFace directly via `--model=google/gemma-3-1b-it`
- A shared NFS PVC (`model-cache-pvc`) caches downloads across pod restarts
- First-time cold start: ~5-10min on a 100Mbps connection; subsequent loads: ~30s

**Production upgrade path:**
1. Use **OCI artifacts** — push models to an internal Harbor/ECR registry
2. KServe modelcar pattern — sidecar mounts model from OCI image
3. This avoids depending on HuggingFace availability and removes external network egress

### 2.4 HAMi over time-slicing or MPS

NVIDIA offers three GPU-sharing approaches:

| Mechanism | Isolation | Pros | Cons |
|---|---|---|---|
| **Time-slicing** (NVIDIA device plugin) | None | Simplest, no extra software | All pods see full GPU memory; OOM possible |
| **MPS** (Multi-Process Service) | Process-level | Better latency for concurrent requests | No vRAM isolation; complex setup |
| **MIG** (Multi-Instance GPU) | Hardware-level | True isolation, kernel-enforced | A100/H100/L40+ only — not RTX 3080 |
| **HAMi** | Software-level | vRAM + compute % isolation; works on consumer GPUs | Slight overhead from LD_PRELOAD hooks |

For consumer GPU (RTX 3080), HAMi is the only option providing real vRAM isolation.
The LD_PRELOAD overhead is typically <5% on inference workloads (measured in your
prior PoC).

### 2.5 LiteLLM as single gateway

Rather than each model exposing its own endpoint that clients hit directly, we route
everything through LiteLLM. This gives:
- One OpenAI-compatible API surface regardless of backend (vLLM, OpenAI, Anthropic, ...)
- Per-team budgets and rate limiting
- Unified Langfuse logging from one place
- Easy A/B testing — alias `gpt-4-equivalent` → swap backend without client changes

**Honest critique:** LiteLLM adds latency (one network hop) and is a single point of
failure. For low-latency inference (<100ms), consider:
- Deploying LiteLLM with HPA (multi-replica)
- Direct-routing critical clients to model endpoints, using LiteLLM only for chat UI

### 2.6 KRO for the abstraction

Could we have used Helm + values.yaml templating instead of KRO? Yes. Why KRO:
- **Declarative composition** — KRO RGDs are first-class CRDs, visible via `kubectl get`
- **Status aggregation** — KRO exposes a unified status from all child resources
- **Reconciliation** — KRO watches children and recreates them if drift detected
- **Type-checked schema** — `gpuMemMb: integer | default=5000` is validated

Helm gives you templating; KRO gives you a real CRD with proper controller semantics.

**Honest critique:** KRO is alpha (v1alpha1 API). For production today, alternatives:
- Kubernetes Composition API (vendor-locked to certain providers)
- Crossplane v2 (more mature but heavier)
- Custom operator written in Go with Kubebuilder

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ Developer                                                            │
│   git push workloads/new-model.yaml                                  │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ ArgoCD                                                               │
│   Application "workloads" detects new file → apply to cluster        │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼ creates InferenceEndpoint CR
┌──────────────────────────────────────────────────────────────────────┐
│ KRO Controller (watches InferenceEndpoint kind)                      │
│   Expands the CR using ResourceGraphDefinition into 2 child objects: │
│     • KServe InferenceService (with vLLM ServingRuntime)             │
│     • Kubernetes Job (registers model with LiteLLM)                  │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ KServe Controller                                                    │
│   Creates Deployment + Service + HPA                                 │
│   Pod schedulerName=hami-scheduler                                   │
│   Resource request: nvidia.com/gpu:1, gpumem:5000, gpucores:50       │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ HAMi Scheduler                                                       │
│   Finds a node where the requested vGPU slice fits                   │
│   Tags pod with vGPU UUID                                            │
│   HAMi device plugin's LD_PRELOAD enforces limits at CUDA runtime    │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ vLLM Pod                                                             │
│   Downloads model from HuggingFace (cached to NFS PVC)               │
│   Starts OpenAI-compatible server on :8000                           │
│   Self-registers via Job → LiteLLM /model/new                        │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ LiteLLM Proxy                                                        │
│   /v1/models lists all registered models                             │
│   /v1/chat/completions routes to the model pod via internal DNS      │
│   Async callback → Langfuse for tracing                              │
└─────────────────────────┬────────────────────────────────────────────┘
                          │
                          ▼
                  ┌───────┴───────┐
                  ▼               ▼
        ┌─────────────────┐  ┌──────────────────┐
        │ Open WebUI      │  │ Langfuse         │
        │ Chat interface  │  │ Traces dashboard │
        └─────────────────┘  └──────────────────┘
```

---

## 4. Data flow per inference request

```
User types message in Open WebUI
  └─> POST /v1/chat/completions to LiteLLM (Service: litellm:4000)
        ├─> LiteLLM looks up model "gemma-1b-fast" in PostgreSQL
        ├─> Routes to gemma-1b-predictor.inference.svc.cluster.local
        │     └─> vLLM inference on RTX 3080 (vGPU slice 5GB)
        ├─> Receives response
        ├─> Async: POST trace to Langfuse (queued, non-blocking)
        └─> Returns to Open WebUI

Total latency budget (10B params, 100 output tokens):
  - Network (UI → LiteLLM → vLLM): ~5ms
  - vLLM prefill (~200 input tokens):  ~150ms
  - vLLM decode (100 tokens @ 80 tok/s): ~1250ms
  - LiteLLM overhead + Langfuse: ~10ms
  ─────────────────────────────────────────
  - Total: ~1.4s for first-token (TTFT ~150ms)
```

---

## 5. Trade-offs accepted in this PoC

| Trade-off | Why accepted | Production fix |
|---|---|---|
| HuggingFace as model source | Simple, no extra infra | Internal Harbor registry + OCI modelcar |
| Single PostgreSQL for LiteLLM + Langfuse | Resource savings on small cluster | Separate DBs or CloudNativePG cluster |
| Master key in plain Secret | PoC speed | External Secrets Operator + Vault |
| No mTLS between services | Simpler | Istio service mesh with strict mTLS |
| Manual Langfuse key wiring | One-time setup | Operator-based or init job that creates project + key |
| AWQ quantization without verification | Trust HF community quants | Re-quantize internally with calibration dataset |
| No model gating/auth on inference pods | All pods accessible cluster-wide | NetworkPolicies restricting access to LiteLLM only |

---

## 6. Security considerations

**Threat model for PoC:** assumes trusted cluster (homelab, single-tenant).

For production, address:

1. **Model pod isolation** — NetworkPolicy that allows ingress only from LiteLLM
2. **API key management** — virtual keys per team, not the master key
3. **Prompt injection logging** — Langfuse can capture, but no automatic flagging
4. **vRAM side-channels** — HAMi isolates allocation but doesn't prevent timing
   attacks via shared GPU compute. For sensitive multi-tenant: dedicate physical GPUs.
5. **Outbound HF traffic** — vLLM downloads from huggingface.co. Add egress
   NetworkPolicy whitelisting only HF + ECR domains.
6. **Secret rotation** — `LITELLM_MASTER_KEY` is shared across many components.
   Rotating it requires coordinated restarts. Better: per-component virtual keys.

---

## 7. Observability gaps

What this PoC covers:
- Pod metrics (CPU, memory) via kube-state-metrics
- GPU metrics (vRAM, util) via HAMi exporter
- vLLM metrics (tokens/sec, prefix-cache hit rate) via vLLM Prometheus
- LiteLLM metrics (latency, error rate, cost) via LiteLLM Prometheus
- LLM traces (prompts, responses, latency breakdown) via Langfuse

What's missing:
- **Distributed tracing** — no OpenTelemetry spans from LiteLLM → vLLM
  (relevant to your QSINT work on OTel/Jaeger). Add via `OTEL_EXPORTER_OTLP_ENDPOINT`
  on LiteLLM + vLLM containers and a Tempo/Jaeger backend.
- **GPU process-level metrics** — DCGM exporter would give per-process util.
- **Cost tracking** — LiteLLM has cost calc for OpenAI models; needs custom rates
  for self-hosted (compute amortization).

---

## 8. Scaling beyond this PoC

If/when you move this to QSINT prod with 500 users and H200 GPUs:

| Component | Change |
|---|---|
| HAMi → MIG | Use real hardware MIG slices on H200 (1g.18gb, 2g.35gb, 3g.71gb partitions) |
| KServe RawDeployment → distributed | `workers: 4` with tensor parallelism via Ray |
| Single PostgreSQL → CloudNativePG | HA cluster with PITR backups |
| LiteLLM 1 replica → HPA | 3-5 replicas with shared PG + Redis cache |
| Open WebUI → custom UI | Build into QSINT main UI; keep Open WebUI as admin tool |
| ArgoCD apps → ApplicationSet | Each model becomes a Helm release auto-discovered from `workloads/` dir |
| KRO RGD v1alpha1 → mature operator | Re-evaluate KRO maturity vs. Crossplane in 6 months |

---

## 9. Open questions / TODOs

- [ ] **AWQ availability for SmolLM3-3B** — verify HF Hub has an AWQ variant.
  Fallback: use base model with reduced max_model_len.
- [ ] **vLLM 0.6.3 + Gemma 3 support** — Gemma 3 architecture support landed in
  vLLM 0.6.2+. Pin verified versions.
- [ ] **KServe + HAMi compatibility** — KServe may inject its own scheduler annotation
  via webhook. Verify our `schedulerName: hami-scheduler` survives. If not, mutate
  via Kyverno policy or use HAMi's mutating webhook to set it automatically.
- [ ] **Langfuse key bootstrap** — currently manual after Langfuse first-time setup.
  Add a one-shot init Job that POSTs to Langfuse's API to create project + keys and
  writes them back to the LiteLLM secret.
- [ ] **HAMi metrics ServiceMonitor labels** — confirm `release: prometheus` matches
  your Prometheus selector.
