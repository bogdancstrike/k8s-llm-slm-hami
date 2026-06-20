# Deploying New Models

> Companion docs: [architecture.md](architecture.md) (how it works) ·
> [documentation.md](documentation.md) (install & operations) ·
> [../README.md](../README.md) (overview)

This is the complete guide to adding, updating, and removing models on the
platform. The unit of deployment is a single **`InferenceEndpoint`** custom
resource — you write one YAML, and KRO + KServe + HAMi do the rest.

---

## Table of contents

1. [The mental model](#1-the-mental-model)
2. [What happens when you apply an InferenceEndpoint](#2-what-happens-when-you-apply-an-inferenceendpoint)
3. [Full field reference](#3-full-field-reference)
4. [Choosing a backend](#4-choosing-a-backend)
5. [Multi-GPU / tensor parallelism](#5-multi-gpu--tensor-parallelism)
6. [Add a GPU model (vLLM)](#6-add-a-gpu-model-vllm)
7. [Add a CPU model (llama.cpp)](#7-add-a-cpu-model-llamacpp)
8. [The LiteLLM registration flow](#8-the-litellm-registration-flow)
9. [Sizing guide](#9-sizing-guide)
10. [Verify a new model](#10-verify-a-new-model)
11. [Update, swap, or remove a model](#11-update-swap-or-remove-a-model)
12. [GPU → CPU fallback](#12-gpu--cpu-fallback)
13. [Troubleshooting deployments](#13-troubleshooting-deployments)
14. [Quick reference](#14-quick-reference)

---

## 1. The mental model

Models are defined as `InferenceEndpoint` CRs in the `inference` namespace. The
bundled examples live in `charts/ai-models/templates/` — one file per model.
You add a model by dropping a new template into that chart and re-applying it
(or by `kubectl apply`-ing a standalone CR).

```text
charts/ai-models/templates/
├── _helpers.tpl                     the shared register-Job template
├── 00-model-cache-pvc.yaml          shared model cache (don't touch)
├── gemma-1b.yaml                    example GPU model (vLLM)
├── smollm3-3b.yaml                  example GPU model (vLLM)
└── qwen25-3b-cpu.yaml               example CPU model (llama.cpp)
```

Each model file is **self-contained**: it holds the `InferenceEndpoint` plus a
one-line `{{ include "ai-models.registerJob" … }}` that renders the LiteLLM
registration Job for that model (using the shared `litellmUrl` / `registerImage`
/ `namespace` from `values.yaml`). To add a model you copy one file, edit it,
and apply — nothing else to touch. Models are **not** listed in `values.yaml`.

One `InferenceEndpoint` becomes a live, GPU-partitioned, OpenAI-compatible,
chat-accessible model. You never write a Deployment, Service, HPA, or
ServingRuntime by hand.

---

## 2. What happens when you apply an InferenceEndpoint

```text
InferenceEndpoint (you write this)
   │  KRO inference-endpoint RGD expands it
   ▼
KServe InferenceService
   ├─ modelFormat + runtime selected from spec.backend
   ├─ env vars (vLLM + llama.cpp; runtime ignores the irrelevant set)
   └─ resources (GPU only when backend=vllm)
        │  KServe renders:
        ▼
Deployment → Pod (+ Service :80→:8000/8080, HPA)
   │  vLLM/llama.cpp loads weights
   ▼
Registration Job → POST /model/new on LiteLLM
   │
   ▼
Model alias appears in /v1/models and Open WebUI's dropdown
```

KRO surfaces progress on the CR itself:

```bash
microk8s kubectl -n inference get inferenceendpoint <name> \
  -o jsonpath='{.status.endpointUrl}{"\n"}'
```

Full internals are in
[architecture.md §4](architecture.md#4-low-level-design).

---

## 3. Full field reference

The schema is defined by the `inference-endpoint` RGD
(`charts/kro-templates/templates/inference-endpoint-rgd.yaml`). All fields
are optional except `litellmAlias`; defaults are applied by KRO.

### Common (both backends)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `backend` | string | `"vllm"` | `vllm` (GPU) or `llamacpp` (CPU). Selects runtime + model format. |
| `servedName` | string | `"default"` | Internal name passed to vLLM `--served-model-name` / llama.cpp `--alias`. Used as the `openai/<servedName>` model in LiteLLM. |
| `litellmAlias` | string | **required** | Public alias clients use in `/v1/chat/completions` and that shows in Open WebUI. |
| `cpuRequest` | string | `"2"` | Pod CPU request. |
| `cpuLimit` | string | `"4"` | Pod CPU limit. |
| `memoryRequest` | string | `"4Gi"` | Pod memory request. |
| `memoryLimit` | string | `"8Gi"` | Pod memory limit. |
| `minReplicas` | integer | `1` | HPA floor. |
| `maxReplicas` | integer | `1` | HPA ceiling. |

### vLLM-specific (`backend: vllm`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | `""` | HuggingFace model ID (e.g. `Qwen/Qwen2.5-0.5B-Instruct-AWQ`). **Required for vLLM.** |
| `gpuCards` | integer | `1` | Number of GPU cards for tensor parallelism (vLLM `--tensor-parallel-size`). Requests this many `nvidia.com/gpu` devices. See [§5](#5-multi-gpu--tensor-parallelism). |
| `gpuMemMb` | integer | `5000` | vRAM budget per card (documentation/back-compat; HAMi v2.8 enforces via `gpuMemPercentage`). |
| `gpuMemPercentage` | integer | `45` | **Hard** vRAM cap per card, as a % of the physical GPU. |
| `gpuCorePercent` | integer | `50` | **Soft** compute share per card, %. |
| `quantization` | string | `"awq"` | vLLM `--quantization` (`awq`, `gptq`, `fp8`, …). |
| `maxModelLen` | integer | `2048` | Context window (`--max-model-len`). Grows KV-cache vRAM. |
| `gpuMemUtilization` | string | `"0.85"` | vLLM `--gpu-memory-utilization` fraction of the *slice*. |

### llama.cpp-specific (`backend: llamacpp`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `modelFile` | string | `""` | GGUF filename inside the model-cache PVC (`/models/<modelFile>`). |
| `modelUrl` | string | `""` | Direct download URL for the GGUF; fetched on first cold start if absent. |
| `ctxSize` | integer | `2048` | Context window (`--ctx-size`). |
| `threads` | integer | `6` | `--threads`; set to **physical** cores (4–8 sweet spot). |
| `batchSize` | integer | `512` | Prompt-processing batch size (`--batch-size`). |

### Status (read-only)

| Field | Source |
|---|---|
| `status.endpointUrl` | `kserveInferenceService.status.url` |

Registration status is the per-model `<name>-litellm-register` Job's completion
(`kubectl -n inference get job <name>-litellm-register`), not a CR status field.

---

## 4. Choosing a backend

| Use **llama.cpp** (CPU) when… | Use **vLLM** (GPU) when… |
|---|---|
| traffic < 1 req/s sustained | traffic > 1 req/s |
| model ≤ 7B | model > 7B |
| latency-tolerant (background/async) | user-facing (interactive chat) |
| no GPU available | TTFT must be sub-500 ms |
| cost matters more than latency | you need maximum throughput |
| exotic quants (GGUF Q2_K, Q3_K_S…) | standard quants (AWQ, GPTQ, FP16, FP8) |

**Performance reference** (Qwen2.5 3B Q4_K_M on a modern Xeon/EPYC w/ AVX-512):
15–30 tok/s generation, TTFT 100–300 ms, ~3 GB RAM, 30–60 s first cold start.
vLLM on the RTX 3080 is ~4× faster; on an L40S ~10×.

The two backends coexist in one mixed Open WebUI dropdown; LiteLLM routes each
alias to the right backend transparently.

---

## 5. Multi-GPU / tensor parallelism

`spec.gpuCards` (default `1`) spreads a single model across N GPU cards using
vLLM tensor parallelism. Setting `gpuCards: N`:

1. requests **N** `nvidia.com/gpu` devices (request and limit), and
2. passes `--tensor-parallel-size=N` to vLLM (via the `TENSOR_PARALLEL_SIZE`
   env var), while
3. applying `gpuMemPercentage` / `gpuCorePercent` to **each** of the N cards.

```yaml
spec:
  backend: vllm
  model: "Qwen/Qwen2.5-32B-Instruct-AWQ"
  gpuCards: 2            # shard across 2 GPUs
  gpuMemPercentage: 90   # per card
  gpuCorePercent: 90     # per card
```

> **Requirement.** `gpuCards: N` needs **N distinct schedulable GPU devices** —
> multiple physical cards, or HAMi configured to advertise multiple vGPU
> devices. On the single-GPU reference node, any value > 1 leaves the pod
> `Pending` with `Insufficient nvidia.com/gpu`. Keep `gpuCards: 1` there.

Tensor parallelism is for models too large for one card, or to raise throughput
on multi-GPU nodes. It is **not** a substitute for HPA replicas (which scale
*out* identical copies for concurrency). See
[architecture.md §8](architecture.md#8-scaling-to-production) for the L40S path.

---

## 6. Add a GPU model (vLLM)

End-to-end for `Qwen2.5-7B-Instruct-AWQ`:

```bash
# 1. Drop a new template into the ai-models chart
cat > charts/ai-models/templates/qwen25-7b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: qwen25-7b
  namespace: inference
spec:
  backend: vllm
  model: "Qwen/Qwen2.5-7B-Instruct-AWQ"
  servedName: "qwen25-7b"
  gpuCards: 1             # bump to 2 on a multi-GPU node for a larger model
  gpuMemMb: 8000          # ~7B AWQ + KV ≈ 7 GB; tight on a 10 GB card
  gpuMemPercentage: 80
  gpuCorePercent: 80
  quantization: "awq"
  maxModelLen: 4096
  gpuMemUtilization: "0.85"
  cpuRequest: "2"
  cpuLimit: "4"
  memoryRequest: "8Gi"
  memoryLimit: "16Gi"
  minReplicas: 1
  maxReplicas: 1
  litellmAlias: "qwen25-7b-coder"
# Self-contained: append the register-Job include so applying this one file
# both deploys and registers the model. Shared bits come from values.yaml.
{{- include "ai-models.registerJob" (dict
      "name" "qwen25-7b" "alias" "qwen25-7b-coder" "served" "qwen25-7b"
      "backend" "vllm" "desc" "Qwen2.5-7B-Instruct-AWQ via KServe + vLLM GPU"
      "root" $) }}
EOF

# 2. Apply (no other file to edit)
./scripts/deploy.sh                 # full idempotent re-run
# or just the models chart:
microk8s helm3 upgrade --install ai-models charts/ai-models -n inference

# 3. Watch reconciliation
microk8s kubectl -n inference get inferenceendpoint qwen25-7b -w
microk8s kubectl -n inference get inferenceservices
microk8s kubectl -n inference logs -f deploy/qwen25-7b-predictor

# 4. Once Ready, the register Job POSTs to LiteLLM; the alias appears in
#    /v1/models and Open WebUI automatically.
```

For a **gated** HF model, load a token first (see
[documentation.md §4](documentation.md#4-installation)).

---

## 7. Add a CPU model (llama.cpp)

```bash
cat > charts/ai-models/templates/phi35-cpu.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: phi35-cpu
  namespace: inference
spec:
  backend: llamacpp
  modelFile: "phi-3.5-mini-instruct-q4_k_m.gguf"
  modelUrl: "https://huggingface.co/.../phi-3.5-mini-instruct-q4_k_m.gguf"
  servedName: "phi35-cpu"
  ctxSize: 2048
  threads: 6              # physical cores
  batchSize: 512
  cpuRequest: "4"
  cpuLimit: "8"
  memoryRequest: "4Gi"
  memoryLimit: "8Gi"
  minReplicas: 1
  maxReplicas: 1
  litellmAlias: "phi35-cpu"
EOF

microk8s helm3 upgrade --install ai-models charts/ai-models -n inference
microk8s kubectl -n inference logs -f deploy/phi35-cpu-predictor
```

The pod runs under the **default** kube-scheduler (no GPU). On first boot it
`wget`s the GGUF into the shared cache PVC (30–60 s); later boots `mmap` it in
seconds.

---

## 8. The LiteLLM registration flow

Each model becomes usable only after it is registered with LiteLLM. A
registration Job (`<name>-litellm-register`, namespace `inference`):

1. polls `http://<name>-predictor.inference.svc.cluster.local/v1/models` until
   it returns 200 (so it never races KServe readiness);
2. `POST`s to `http://litellm.ai-platform.svc:4000/model/new` with
   `Authorization: Bearer $LITELLM_MASTER_KEY`, mapping the alias to the
   predictor's `/v1` endpoint;
3. LiteLLM validates the key, inserts the row, and hot-reloads its router (no
   restart);
4. the Job exits 0 and is GC'd an hour later by `ttlSecondsAfterFinished`.

The master key reaches the `inference` namespace via the secret mirror created
by the `litellm` chart (Secrets are namespace-scoped). Full sequence in
[architecture.md §4.6](architecture.md#46-litellm-model-registration-sequence).

---

## 9. Sizing guide

Rules of thumb on the RTX 3080 (10 GB):

| Model size | Quant | Approx vRAM slice |
|---|---|---|
| 1B | AWQ INT4 | 2.5–3 GB |
| 3B | AWQ INT4 | 4–5 GB |
| 7B | AWQ INT4 | 7–8 GB |
| 13B | AWQ INT4 | ≥ 12 GB — won't fit on one card (use `gpuCards: 2`) |

Slice budget ≈ weights + KV cache (grows with `maxModelLen`) + CUDA context +
activations. If vLLM logs `CUDA out of memory`, lower `gpuMemUtilization` to
`"0.75"`, shrink `maxModelLen`, or raise `gpuMemPercentage`/`gpuCards`.

---

## 10. Verify a new model

```bash
LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')"

# Is the alias registered?
curl -s http://litellm.local.ro/v1/models \
  -H "Authorization: Bearer $LITELLM_KEY" | jq '.data[].id'

# Does it answer?
curl -s http://litellm.local.ro/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen25-7b-coder","messages":[{"role":"user","content":"Say hi"}],"max_tokens":16}' \
  | jq -r '.choices[0].message.content'
```

Or run the full end-to-end smoke suite (it auto-discovers every registered
alias):

```bash
LITELLM_KEY="$LITELLM_KEY" python3 tests/e2e_smoke.py
```

See [documentation.md §10](documentation.md#10-testing) for the test harness.

---

## 11. Update, swap, or remove a model

**Change parameters / swap weights.** Edit the template and re-apply. LiteLLM's
`/model/new` is keyed on `model_info.id`; the bundled register jobs `DELETE`
then re-create the row, so a changed `servedName`/`api_base` is picked up
cleanly. After swapping the underlying model, roll the predictor:

```bash
microk8s kubectl -n inference rollout restart deploy/<name>-predictor
```

**Force a re-reconcile** (e.g. after editing the RGD):

```bash
microk8s kubectl -n inference annotate inferenceendpoint <name> \
  kro.run/force-reconcile="$(date +%s)" --overwrite
```

**Remove a model.** Delete the template and re-apply the chart (Helm prunes the
CR), or delete the CR directly — KRO garbage-collects the InferenceService, pod,
Service, and HPA:

```bash
microk8s kubectl -n inference delete inferenceendpoint <name>
# then remove it from LiteLLM:
curl -s -X POST http://litellm.local.ro/model/delete \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"id":"<name>"}'
```

---

## 12. GPU → CPU fallback

A production LiteLLM pattern: register a GPU primary and a CPU twin, then let
the router fall back automatically on timeout/5xx.

```yaml
model_list:
  - model_name: "qwen-chat"
    litellm_params:
      model: "openai/qwen25-3b-gpu"
      api_base: "http://qwen25-3b-predictor.inference.svc.cluster.local/v1"
      api_key: "dummy"
  - model_name: "qwen-chat-fallback"
    litellm_params:
      model: "openai/qwen25-3b-cpu"
      api_base: "http://qwen25-3b-cpu-predictor.inference.svc.cluster.local/v1"
      api_key: "dummy"

router_settings:
  fallbacks:
    - {"qwen-chat": ["qwen-chat-fallback"]}
  num_retries: 1
  request_timeout: 30
```

The client asks for `qwen-chat`; LiteLLM tries GPU and, on failure, retries CPU
transparently.

---

## 13. Troubleshooting deployments

| Symptom | Likely cause & fix |
|---|---|
| Pod `Pending`, `Insufficient nvidia.com/gpumem` | HAMi can't fit the slice (or device plugin down). Lower `gpuMemPercentage`, free another model, or check `gpu=on` + driver + containerd `nvidia` runtime. |
| Pod `Pending`, `Insufficient nvidia.com/gpu` | `gpuCards` > available GPU devices. Set `gpuCards: 1` on a single-GPU node. |
| vLLM: `OSError: model is gated` | Missing/invalid HF token. Recreate the `huggingface-token` secret in `inference`. |
| vLLM: `CUDA out of memory` | Lower `gpuMemUtilization` to `"0.75"`, shrink `maxModelLen`, or raise `gpuMemPercentage`. |
| `default chat template is no longer allowed` | Tokenizer ships no chat template; runtime falls back to ChatML. If the pod predates the fallback, `rollout restart` it. |
| Pod `Ready` but not in `/v1/models` | Register Job failed — usually a `LITELLM_MASTER_KEY` mismatch or LiteLLM not Ready when it ran. Re-run it (below). |
| Open WebUI says "no models" | `/v1/models` is empty → registration never succeeded. Walk back the register Job. |

Re-run a registration Job:

```bash
microk8s kubectl -n inference delete job <name>-litellm-register
microk8s kubectl -n inference annotate inferenceendpoint <name> \
  kro.run/force-reconcile="$(date +%s)" --overwrite
microk8s kubectl -n inference logs job/<name>-litellm-register
```

---

## 14. Quick reference

```bash
# List endpoints, services, runtimes
microk8s kubectl get inferenceendpoints -A
microk8s kubectl get inferenceservices -A
microk8s kubectl get clusterservingruntime

# Status of one model
microk8s kubectl -n inference get inferenceendpoint <name> -o yaml | yq '.status'

# Apply the models chart only
microk8s helm3 upgrade --install ai-models charts/ai-models -n inference

# Registered aliases
curl -s http://litellm.local.ro/v1/models \
  -H "Authorization: Bearer $LITELLM_KEY" | jq '.data[].id'
```
