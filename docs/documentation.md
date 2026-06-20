# AI Platform — Documentation (Install & Operations)

> Companion docs: [architecture.md](architecture.md) (design & internals) ·
> [deploy_new_models.md](deploy_new_models.md) (adding models) ·
> [../README.md](../README.md) (overview & quick start)

This is the operator/user manual: how to install, access, observe, test,
operate, troubleshoot, and harden the platform. For *why* it is built the way it
is, read [architecture.md](architecture.md).

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [Component & namespace reference](#4-installation--component-reference)
5. [Local UIs & credentials](#5-local-uis--credentials)
6. [Using the platform](#6-using-the-platform)
7. [Observability](#7-observability)
8. [Verifying GPU sharing](#8-verifying-gpu-sharing)
9. [Reading traces in Jaeger](#9-reading-traces-in-jaeger)
10. [Testing](#10-testing)
11. [Operations cheat-sheet](#11-operations-cheat-sheet)
12. [Troubleshooting](#12-troubleshooting)
13. [Production hardening checklist](#13-production-hardening-checklist)
14. [Upgrades, teardown & redeploy](#14-upgrades-teardown--redeploy)
15. [Repo layout](#15-repo-layout)
16. [References](#16-references)

---

## 1. What this is

A self-service AI platform for a single-node MicroK8s box with one NVIDIA GPU. A
developer applies an `InferenceEndpoint` YAML and gets a live, GPU-partitioned,
observable, chat-accessible model in minutes.

| | |
|---|---|
| Target cluster | MicroK8s, single node, `gpu=on` label |
| GPU | NVIDIA RTX 3080 10 GB, vGPU-partitioned by HAMi |
| Install | Helm charts in `charts/`, applied by `scripts/deploy.sh` |
| Runtimes | KServe `ClusterServingRuntime` — vLLM (GPU) + llama.cpp (CPU) |
| Abstraction | KRO `InferenceEndpoint` CRD (one YAML per model) |
| Gateway | LiteLLM (OpenAI-compatible) |
| Chat UI | Open WebUI |
| Observability | kube-prometheus-stack, Grafana, OTel Collector, Jaeger, Langfuse |

---

## 2. Prerequisites

### Hardware

- Linux node with an NVIDIA GPU (Ampere or newer, ≥ 10 GB vRAM).
- 32 GB+ RAM recommended.
- 100 GB+ free disk for the model-cache PVC.

### Node software

| Component | Tested version |
|---|---|
| MicroK8s | 1.28+ (`microk8s helm3` enabled) |
| NVIDIA driver | 535+ |
| nvidia-container-toolkit | latest |
| containerd | 1.7+ with `nvidia` runtime set as default |

Required MicroK8s addons: `dns`, `hostpath-storage`, `ingress`, `helm3`,
`rbac`, and the NVIDIA GPU support (HAMi is installed by the script; the NVIDIA
driver/device plugin must be present on the host).

### Cluster expectations

- Single node, node name `bogdan` (or override `GPU_NODE` for
  `scripts/deploy.sh`).
- The node carries `gpu=on` (the script sets it).
- `*.local.ro` hostnames resolve to the node — use
  `scripts/update-local-hosts.sh`.

---

## 3. Installation

```bash
# 1. Map local hostnames to 127.0.0.1 (once per workstation)
sudo ./scripts/update-local-hosts.sh

# 2. Install the whole stack (idempotent — re-run = upgrade)
./scripts/deploy.sh

# 3. (Optional) load a Hugging Face token for gated models
microk8s kubectl -n inference patch secret huggingface-token --type merge \
  -p '{"stringData":{"token":"hf_xxx"}}'
#    …or pass it to the script up front:
HUGGINGFACE_TOKEN=hf_xxx ./scripts/deploy.sh
```

### What the script does

`scripts/deploy.sh` is the canonical install. It:

1. Confirms intent (interactive, or `--yes` / `CONFIRM=yes`).
2. Tears down any prior PoC releases/namespaces/CRDs (skip with
   `SKIP_TEARDOWN=1`). It leaves unrelated releases alone (use
   `WIPE_EXTRA="loki tempo"` to also remove named extras).
3. Adds upstream Helm repos and labels the GPU node.
4. Runs `helm dependency update` for the wrapper charts.
5. Installs one release per app, in dependency order:

   ```text
   cert-manager → kube-prometheus-stack → HAMi → KRO →
   KServe CRDs → KServe → namespaces →
   postgresql → litellm → langfuse → otel-collector → jaeger →
   open-webui → serving-runtimes → monitoring →
   kro-templates → ai-models
   ```

6. Prints a summary with access URLs and live credentials.

Expect 15–30 minutes the first time, mostly pulling images and the first model
weights from Hugging Face.

### Environment variables

| Var | Default | Effect |
|---|---|---|
| `KUBECTL` | `microk8s kubectl` | kubectl command |
| `HELM` | `microk8s helm3` | helm command |
| `GPU_NODE` | `bogdan` | node labelled `gpu=on` |
| `HUGGINGFACE_TOKEN` | — | if set, patches the `huggingface-token` secret in `inference` |
| `WIPE_EXTRA` | — | extra releases to uninstall during teardown |
| `SKIP_TEARDOWN` | — | skip the destructive phase (install/upgrade only) |
| `CONFIRM` / `--yes` | — | skip the interactive confirmation |

---

## 4. Installation & component reference

### Namespaces

| Namespace | Contents |
|---|---|
| `cert-manager` | cert-manager controller + CRDs |
| `kserve` | KServe controller, webhooks, CRDs |
| `kro-system` | KRO controller + RGDs |
| `kube-system` | HAMi scheduler + device-plugin DaemonSet |
| `observability` | kube-prometheus-stack + bundled dashboards/ServiceMonitors |
| `ai-platform` | LiteLLM, Open WebUI, Langfuse, PostgreSQL, Jaeger, OTel Collector, ingresses |
| `inference` | `InferenceEndpoint`s, `InferenceService`s, model pods, register Jobs, runtimes, model-cache PVC |

### Releases (one per app)

See [architecture.md §7](architecture.md#7-chart--release-topology) for the full
chart→release table and ordering rationale.

---

## 5. Local UIs & credentials

```bash
sudo ./scripts/update-local-hosts.sh   # maps *.local.ro → 127.0.0.1
```

| UI / endpoint | URL | Username | Secret |
|---|---|---|---|
| Grafana | http://grafana.local.ro | `admin` | see below |
| Open WebUI | http://open-webui.local.ro | first signup → admin | chosen at signup |
| Langfuse | http://langfuse.local.ro | first signup → admin | chosen at signup |
| Jaeger | http://jaeger.local.ro | n/a | n/a |
| LiteLLM API | http://litellm.local.ro | Bearer auth | `LITELLM_MASTER_KEY` |

```bash
# Grafana admin password
microk8s kubectl -n observability get secret kube-prom-stack-grafana \
  -o go-template='{{index .data "admin-password" | base64decode}}{{"\n"}}'

# LiteLLM master key
microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}{{"\n"}}'
```

- Open WebUI and Langfuse have no chart-defined users — **the first account
  created becomes admin.**
- LiteLLM is bearer-auth: `Authorization: Bearer <LITELLM_MASTER_KEY>`.
- The default master key is `sk-litellm-master-change-me` — **rotate before any
  non-lab use.**

---

## 6. Using the platform

### Chat (Open WebUI)

Open http://open-webui.local.ro, create the first (admin) account, and pick a
model from the dropdown. The dropdown lists every alias LiteLLM knows
(GPU + CPU mixed); switching is transparent.

### API (LiteLLM, OpenAI-compatible)

```bash
LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')"

# List models
curl -s http://litellm.local.ro/v1/models \
  -H "Authorization: Bearer $LITELLM_KEY" | jq '.data[].id'

# Chat completion
curl -s http://litellm.local.ro/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gemma-1b-fast","messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'
```

### Models bundled in the PoC

| LiteLLM alias | Backend | HF model | GPU/RAM slice | Context |
|---|---|---|---|---|
| `gemma-1b-fast` | vLLM (GPU/HAMi) | `Qwen/Qwen2.5-Coder-1.5B-Instruct-AWQ` | 40 % vRAM (~4 GB) | 16384 |
| `smollm3-3b-quality` | vLLM (GPU/HAMi) | `Qwen/Qwen2.5-0.5B-Instruct-AWQ` | 40 % vRAM (~4 GB) | 16384 |
| `qwen-3b-cpu` | llama.cpp (CPU) | `Qwen/Qwen2.5-3B-Instruct-GGUF` q4_k_m | ~3 GB RAM | 16384 |

The `gemma-1b-fast` slot serves Qwen2.5-Coder-1.5B (coding-tuned, tool calling
enabled); it replaced TinyLlama, whose 2048-token window was too small for
coding agents. The two GPU models coexist on the 10 GB RTX 3080: 40 % + 40 % vRAM
and 35 % + 35 % cores reserved via HAMi (~80 % of the card). Trim a slice's
`gpuMemPercentage` if you need to add a third GPU model. Each context
window is bounded by what its KV cache fits in the slice — see
[deploy_new_models.md §9](deploy_new_models.md#9-sizing-guide). To add your own,
see [deploy_new_models.md](deploy_new_models.md).

### Use from OpenCode (coding agent)

[OpenCode](https://opencode.ai) and other IDE agents speak to LiteLLM as a plain
OpenAI-compatible endpoint. Config is checked in under `opencode/config/`:

| File | Purpose |
|---|---|
| `opencode.json` | OpenCode provider config — `baseURL`, API key, and one entry per model with a `limit: {context, output}`. |
| `update-config.py` | Regenerates `opencode.json` from LiteLLM's live `/v1/models`, stamping the per-model limits from a built-in table. |

```bash
LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')" \
LITELLM_KEY="$LITELLM_KEY" python3 opencode/config/update-config.py
```

**Why the `limit` matters.** Coding agents inject large tool/system prompts
(~10 k tokens) and otherwise request a huge completion budget (OpenCode defaults
to ~32 k). On a small local model that blows past the KV cache and LiteLLM
returns `ContextWindowExceededError`. The `context`/`output` caps keep every
request inside the served model's window, and **must match** the model's
`maxModelLen` (vLLM) / `ctxSize` (llama.cpp):

| Alias | context | output | Notes |
|---|---|---|---|
| `gemma-1b-fast` | 16384 | 4096 | Qwen2.5-Coder-1.5B — coding-tuned, tool calling on. Best for OpenCode. |
| `smollm3-3b-quality` | 16384 | 4096 | Good general default for OpenCode. |
| `qwen-3b-cpu` | 16384 | 4096 | CPU; higher latency but larger/stronger model. |

If you change a model's `maxModelLen`/`ctxSize`, update both `opencode.json` and
the `MODEL_LIMITS` table in `update-config.py` to keep them in sync.

---

## 7. Observability

| Pillar | Tool | URL |
|---|---|---|
| Metrics | Prometheus + Grafana | http://grafana.local.ro |
| Traces | OTel Collector + Jaeger | http://jaeger.local.ro |
| LLM analytics | Langfuse | http://langfuse.local.ro |

### Grafana dashboards

Auto-imported by the Grafana sidecar from the `monitoring` chart. Key ones:
**HAMi GPU Split — Native Metrics** (per-pod vRAM/compute) and
**AI Platform — vLLM Inference** (requests, TTFT/TPOT, KV-cache, throughput). KServe
ModelServer/TorchServe/Triton/Knative dashboards are bundled but populate only
when a matching runtime is deployed. Details in
[architecture.md §6](architecture.md#6-observability-architecture).

### Langfuse vs Jaeger

Langfuse answers *what* (which prompt, how many tokens, what cost); Jaeger
answers *where the time went* (routing vs prefill vs decode). Use both.

---

## 8. Verifying GPU sharing

The point of HAMi is two pods sharing one physical GPU **with real isolation**.

```bash
# Both GPU pods on the same node
microk8s kubectl -n inference get pods -o wide \
  -l 'serving.kserve.io/inferenceservice in (gemma-1b, smollm3-3b)'

# Each runs under the HAMi scheduler
microk8s kubectl -n inference get pod <pod> -o jsonpath='{.spec.schedulerName}'
# → hami-scheduler

# Inside a pod, nvidia-smi shows only the slice
microk8s kubectl -n inference exec deploy/gemma-1b-predictor -- nvidia-smi

# On the node, both processes coexist on GPU 0
nvidia-smi   # two python procs, combined ~8–10 GB

# Concurrent stress (both models in parallel)
for i in {1..10}; do
  curl -s http://litellm.local.ro/v1/chat/completions \
    -H "Authorization: Bearer $LITELLM_KEY" \
    -d '{"model":"gemma-1b-fast","messages":[{"role":"user","content":"Count to 100"}],"max_tokens":500}' &
  curl -s http://litellm.local.ro/v1/chat/completions \
    -H "Authorization: Bearer $LITELLM_KEY" \
    -d '{"model":"smollm3-3b-quality","messages":[{"role":"user","content":"Count to 100"}],"max_tokens":500}' &
done; wait
```

Grafana's HAMi dashboard should show two distinct per-pod vRAM lines with
neither starving the other.

---

## 9. Reading traces in Jaeger

```bash
curl http://litellm.local.ro/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gemma-1b-fast","messages":[{"role":"user","content":"Hello"}],"max_tokens":50}'

xdg-open http://jaeger.local.ro
```

In the UI: Service `litellm-proxy` → Operation `POST /v1/chat/completions` →
Find Traces. A trace shows the waterfall (routing → prefill → decode); click a
span for attributes (`k8s.pod.name`, `llm.model`, `llm.prompt_tokens`, …). CPU
(llama.cpp) models currently trace only up to the LiteLLM → pod boundary (no
OTLP from llama.cpp yet).

---

## 10. Testing

The repo ships an end-to-end / integration suite under `tests/` that exercises
**every component** — control plane, gateway, each deployed model, and all four
UIs (Grafana, Jaeger, Langfuse, Open WebUI). It is **standard-library only**
(no `pip install`).

```bash
LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')" \
python3 tests/e2e_smoke.py
```

What it covers (high level — see [`../tests/README.md`](../tests/README.md) for
the full matrix and flags):

- **Gateway:** LiteLLM health, `/v1/models` lists the expected aliases, and a
  real completion from **each** registered model.
- **UIs:** Grafana, Jaeger, Langfuse, Open WebUI reachable and healthy.
- **Observability integration:** Prometheus targets up, the model traces land in
  Jaeger, and completions are logged to Langfuse.
- **Cluster (optional):** when `kubectl` is available, pod/Job readiness for each
  component and model.

Useful env/flags: `LITELLM_URL`, `OPEN_WEBUI_URL`, `GRAFANA_URL`, `JAEGER_URL`,
`LANGFUSE_URL`, `TIMEOUT`, `--junit out.xml`, `--no-cluster`.

Two focused suites complement the smoke test:

```bash
# Deep observability — per-model trace correlation across Jaeger + Langfuse
python3 tests/test_observability.py

# Multi-turn chat scenarios — 3 conversations per model (geography, arithmetic,
# coding) checking responses, multi-turn context, per-question latency, and that
# each conversation's trace reaches BOTH Jaeger and Langfuse.
LITELLM_KEY="$LITELLM_KEY" python3 tests/test_chat_scenarios.py
```

`test_chat_scenarios.py` hard-gates platform behaviour (every turn responds,
anchor answers correct, traces present) and reports model answer-quality + timing
(a 0.5B model legitimately flubs chained-reasoning follow-ups). Because every
client (Open WebUI, OpenCode) routes through LiteLLM, its `otel` callback traces
all of them — that's what these tests verify lands in Jaeger and Langfuse.

---

## 11. Operations cheat-sheet

```bash
# KRO
microk8s kubectl get resourcegraphdefinitions
microk8s kubectl get inferenceendpoints -A

# KServe
microk8s kubectl get inferenceservices -A
microk8s kubectl get servingruntime,clusterservingruntime -A

# HAMi
microk8s kubectl -n kube-system logs -l app.kubernetes.io/name=hami-scheduler
microk8s kubectl describe node <node> | grep -A4 'Allocated resources'
microk8s kubectl describe node <node> | grep nvidia.com

# Force reconcile an InferenceEndpoint
microk8s kubectl -n inference annotate inferenceendpoint <name> \
  kro.run/force-reconcile="$(date +%s)" --overwrite

# Re-run a registration Job
microk8s kubectl -n inference delete job <model>-litellm-register
microk8s helm3 upgrade --install ai-models charts/ai-models -n inference

# Quick LiteLLM smoke
curl -s http://litellm.local.ro/v1/models \
  -H "Authorization: Bearer $LITELLM_KEY" | jq '.data[].id'
```

---

## 12. Troubleshooting

### Pod stuck `Pending` — `Insufficient nvidia.com/gpumem`

```bash
microk8s kubectl describe node <node> | grep -A5 Allocatable
microk8s kubectl -n kube-system get pods -l app.kubernetes.io/name=hami
microk8s kubectl -n kube-system logs -l app.kubernetes.io/name=hami-device-plugin
```

Likely: HAMi device plugin not running. Check `gpu=on`, NVIDIA driver loaded,
and containerd's default runtime is `nvidia`.

### vLLM hangs at startup

```bash
microk8s kubectl -n inference logs -f deploy/<model>-predictor
```

- `OSError: model is gated` → HF token missing/invalid; recreate the
  `huggingface-token` secret.
- `CUDA out of memory` → lower `gpuMemUtilization` to `"0.75"` or raise
  `gpuMemPercentage`.
- `ValueError: The model's max seq len (N) is larger than the maximum number of
  tokens that can be stored in KV cache (M)` → `maxModelLen` doesn't fit the GPU
  slice. Lower `maxModelLen` to ≤ M, or raise `gpuMemPercentage` (and re-sync the
  OpenCode `limit`). See [deploy_new_models.md §9](deploy_new_models.md#9-sizing-guide).
- `Connection timeout to Hugging Face` → egress firewall/NetworkPolicy.

### New model pod stuck `Pending` during an upgrade — `CardInsufficientCore`

A predictor rolled with the default surge would run old+new pods at once, each
holding a HAMi `gpucores` slice — exceeding 100 % on this 2-GPU-model box. The
RGD now pins predictors to `deploymentStrategy: Recreate` to prevent this. If you
still hit it (e.g. a hand-edited Deployment), restore `Recreate` or delete the
old pod so the new one can claim the slice:

```bash
microk8s kubectl -n inference get deploy <model>-predictor \
  -o jsonpath='{.spec.strategy.type}{"\n"}'      # expect: Recreate
```

### LiteLLM returns `ContextWindowExceededError` (e.g. from OpenCode)

The client asked for more tokens (prompt + completion) than the model's window.
Fixes: lower the client's `max_tokens`, set/lower the OpenCode `limit` for that
alias, or raise the model's `maxModelLen`/`ctxSize` (see the
[OpenCode section](#use-from-opencode-coding-agent) in §6). TinyLlama
(`gemma-1b-fast`, 2048) can't serve agent-sized prompts — use a Qwen alias.

### Pod `Ready` but not in LiteLLM

```bash
microk8s kubectl -n inference get jobs
microk8s kubectl -n inference logs job/<model>-litellm-register
```

Usually a `LITELLM_MASTER_KEY` mismatch between `litellm-secrets` in
`ai-platform` and the mirror in `inference`, or LiteLLM not Ready when the Job
ran. Re-run the Job (see cheat-sheet).

### `default chat template is no longer allowed`

The tokenizer ships no chat template; the runtime falls back to ChatML. If the
pod predates the fallback: `rollout restart deploy/<model>-predictor`.

### Jaeger shows no traces

```bash
microk8s kubectl -n ai-platform logs deploy/otel-collector | grep -i "received\|trace"
microk8s kubectl -n ai-platform logs deploy/litellm | grep -i "otel\|trace"
```

### Grafana dashboards missing

The sidecar imports any ConfigMap labelled `grafana_dashboard: "1"`.

```bash
microk8s kubectl -n observability get cm -l grafana_dashboard=1
microk8s kubectl -n observability logs <grafana-pod> -c grafana-sc-dashboard
```

Manual fallback: Grafana → Dashboards → Import → paste JSON from
`charts/monitoring/files/dashboards/`.

### Open WebUI says "no models"

`/v1/models` is empty → the registration Jobs never succeeded. Walk back the
"not in LiteLLM" check above.

---

## 13. Production hardening checklist

### Security
- [ ] Secrets via External Secrets Operator + Vault (eliminate hard-coded keys).
- [ ] mTLS between services (Istio strict, or Linkerd).
- [ ] Tight `NetworkPolicies` — inference pods reachable only from LiteLLM.
- [ ] Minimal RBAC per ServiceAccount.
- [ ] `PodSecurity: restricted` on `ai-platform` + `inference` (PoC uses privileged).
- [ ] Image scanning (Trivy/Snyk) in CI.
- [ ] Egress firewall: only Hugging Face + third-party LLM APIs.
- [ ] Virtual API keys per team; never share the master key. Rotate it.

### Reliability
- [ ] HA on critical components (LiteLLM HPA min=2, CloudNativePG, Langfuse 2+, OTel HPA).
- [ ] Postgres backups (pgBackRest), off-site copies.
- [ ] `PodDisruptionBudgets` on all Deployments.
- [ ] Measured (not guessed) requests/limits; tuned probes.

### Observability
- [ ] Persistent traces (Jaeger+ES, or Tempo).
- [ ] Long-term metrics (Thanos/Mimir).
- [ ] Centralised logs (Loki).
- [ ] Alertmanager rules (error rate, latency, GPU temp, cost) + SLOs.

### Model management
- [ ] Internal model registry (Harbor OCI) — drop runtime HF dependency.
- [ ] Modelcar storage initializer; canary via `canaryTrafficPercent`.
- [ ] Versioning + rollback; in-house re-quantisation CI.

### Delivery & cost
- [ ] Sealed/External Secrets for Git; branch protection; pre-commit (`yamllint`, `kubeval`, `conftest`).
- [ ] Per-team cost attribution; budget alerts; GPU-utilisation SLOs; idle scale-down.

---

## 14. Upgrades, teardown & redeploy

- **Upgrade / reconcile:** re-run `./scripts/deploy.sh` — it is
  idempotent and uses `helm upgrade --install` per release. To upgrade only the
  models: `microk8s helm3 upgrade --install ai-models charts/ai-models -n inference`.
- **Install without teardown:** `SKIP_TEARDOWN=1 ./scripts/deploy.sh`.
- **Full teardown + redeploy:** `./scripts/deploy.sh` (the default
  flow tears down first). This loses model pods (weights re-download), Langfuse
  projects/keys, LiteLLM registrations (re-created by Jobs), Jaeger traces
  (in-memory), Grafana credentials (rotated), and the model-cache PVC.
- **Remove extra releases too:** `WIPE_EXTRA="loki tempo" ./scripts/deploy.sh`.

---

## 15. Repo layout

```text
.
├── README.md                         overview & quick start
├── docs/
│   ├── architecture.md               design, internals, scaling
│   ├── deploy_new_models.md          adding/updating/removing models
│   └── documentation.md              this file — install & operations
├── charts/                           Helm charts — source of truth (one per app)
│   ├── namespaces/             ai-platform, inference namespaces
│   ├── cert-manager/           cert-manager wrapper
│   ├── observability-stack/    kube-prometheus-stack wrapper
│   ├── hami/                   HAMi vGPU wrapper
│   ├── kro/                    KRO controller wrapper
│   ├── kserve-crd/             KServe CRDs
│   ├── kserve/                 KServe wrapper
│   ├── kro-templates/          the InferenceEndpoint RGD
│   ├── postgresql/                   shared LiteLLM + Langfuse database
│   ├── litellm/                      LiteLLM gateway + ingress + secret mirror
│   ├── langfuse/                     Langfuse tracing UI + ingress
│   ├── open-webui/                   Open WebUI chat interface + ingress
│   ├── jaeger/                       Jaeger tracing backend + UI + ingress
│   ├── otel-collector/               OpenTelemetry Collector
│   ├── serving-runtimes/             vLLM + llama.cpp ClusterServingRuntimes
│   ├── monitoring/                   Grafana dashboards + HAMi ServiceMonitors
│   └── ai-models/                    example InferenceEndpoints + register Jobs
├── opencode/config/
│   ├── opencode.json                 OpenCode provider + per-model limits
│   └── update-config.py              regenerates opencode.json from LiteLLM
├── scripts/
│   ├── deploy.sh            one-shot installer / upgrader
│   └── update-local-hosts.sh         maps *.local.ro hostnames to 127.0.0.1
└── tests/
    ├── e2e_smoke.py                  stdlib-only e2e/integration suite
    ├── test_observability.py        deep trace-correlation / span tests
    └── README.md
```

---

## 16. References

- KServe — <https://kserve.github.io/website/>
- KRO — <https://kro.run>
- HAMi — <https://github.com/Project-HAMi/HAMi>
- vLLM — <https://docs.vllm.ai>
- llama.cpp server — <https://github.com/ggerganov/llama.cpp/tree/master/examples/server>
- LiteLLM — <https://docs.litellm.ai>
- Langfuse — <https://langfuse.com/docs>
- OpenTelemetry Collector — <https://opentelemetry.io/docs/collector/>
- Jaeger — <https://www.jaegertracing.io/docs/>
