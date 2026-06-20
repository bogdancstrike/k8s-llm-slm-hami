# tests/

End-to-end **and integration** tests for the AI Platform. The suite
exercises *every component* — control plane, gateway, each deployed model, and
all four UIs — using only the Python 3 standard library (no `pip install`).

## Test files

| File | Purpose |
|---|---|
| `e2e_smoke.py` | End-to-end smoke tests: health checks, model chat, cluster readiness, trace pipeline |
| `test_observability.py` | Deep observability tests: per-model trace correlation (Jaeger↔Langfuse), content fidelity, span attributes, tool-param dropping, context windows |

## What it covers

### `e2e_smoke.py`

#### HTTP / e2e tier (no cluster access needed)

| Check | Asserts |
|---|---|
| `litellm/health` | LiteLLM `/health/liveliness` (or `/readiness`) returns 200 |
| `litellm/models-registered` | `/v1/models` lists all expected aliases |
| `litellm/chat[<alias>]` | A real `/v1/chat/completions` returns non-empty content — **for every registered model** (GPU vLLM + CPU llama.cpp) |
| `open-webui/health` | Open WebUI `/health` (or `/`) is reachable |
| `grafana/health` | Grafana `/api/health` reports `database: ok` |
| `jaeger/ui` | Jaeger UI is up and `/api/services` responds |
| `langfuse/health` | Langfuse `/api/public/health` returns 200 (app + DB up) |
| `integration/trace-pipeline` | After driving GPU traffic, a `vllm-inference` trace actually lands in Jaeger — proves OTLP → Collector → Jaeger |
| `integration/trace-correlation` | A request with a known `traceparent` header lands in **both** Jaeger and Langfuse under the same trace_id, with non-empty input/output |

#### Cluster / integration tier (needs `kubectl`; auto-skips otherwise)

| Check | Asserts |
|---|---|
| `cluster/ready[<name>]` | `postgresql`, `litellm`, `langfuse-web`, `open-webui`, `jaeger`, `otel-collector` are Ready |
| `cluster/serving-runtimes` | `vllm-runtime` + `llamacpp-runtime` ClusterServingRuntimes exist |
| `cluster/isvc-ready[<model>]` | Each model's KServe InferenceService is `Ready` |
| `cluster/register-job[<model>]` | Each `<model>-litellm-register` Job Succeeded |
| `cluster/hami-scheduler` | HAMi scheduler pod is Running |
| `cluster/gpu-pods-on-hami` | GPU model pods are scheduled by `hami-scheduler` |
| `integration/prometheus-targets` | Via the Grafana Prometheus datasource proxy, `up` shows targets scraping — proves the Prometheus + Grafana metrics pipeline |

### `test_observability.py`

Deep observability tests exercising the full LiteLLM → OTel Collector → Jaeger + Langfuse pipeline **per model**.

| Check | Asserts |
|---|---|
| `trace-correlation/<alias>` | A request with a known `traceparent` appears in both Jaeger and Langfuse under the same trace_id, with non-empty input/output — **for every model** |
| `langfuse-content/<alias>` | The input stored in Langfuse contains the actual user prompt; the output is non-empty — **for every model** |
| `langfuse/environment` | Langfuse traces carry `environment=default` |
| `langfuse/trace-name` | Langfuse traces have a meaningful name (model name) |
| `langfuse-observations/<alias>` | The Langfuse trace has observations with model names and token usage — **for every model** |
| `jaeger-attributes/<alias>` | Jaeger spans contain `gen_ai.*` semantic convention attributes, including `input.value` and `output.value` mapping — **for every model** |
| `tool-params/<alias>` | Models respond correctly even when Open WebUI-style `tools`/`tool_choice` params are included (proves `drop_params` works) — **for every model** |
| `context-window/<alias>` | Models handle multi-turn conversations (6 messages) within their context window — **for every model** |
| `multiturn/langfuse-trace` | A multi-message conversation produces a Langfuse trace with conversation context in the input |
| `traceid/format-equivalence` | The 32-hex-char trace_id from `traceparent` is stored identically in both Jaeger and Langfuse |

The models under test:

| Alias | Backend | Model |
|---|---|---|
| `gemma-1b-fast` | KServe + vLLM (GPU/HAMi) | TinyLlama 1.1B AWQ |
| `smollm3-3b-quality` | KServe + vLLM (GPU/HAMi) | Qwen2.5 0.5B AWQ |
| `qwen-3b-cpu` | KServe + llama.cpp (CPU) | Qwen2.5 3B GGUF q4_k_m |

## Run

```bash
# Full smoke suite (HTTP + cluster tiers). Host /etc/hosts must map *.local.ro → 127.0.0.1.
LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')" \
python3 tests/e2e_smoke.py

# Observability-only tests (trace correlation, content, attributes)
python3 tests/test_observability.py

# HTTP tier only (e.g. from a workstation without kubectl)
python3 tests/e2e_smoke.py --no-cluster

# Run a subset and emit a JUnit report
python3 tests/e2e_smoke.py --only litellm --only grafana --junit out.xml
python3 tests/test_observability.py --only trace-correlation --junit obs.xml
```

Exit code is `0` when nothing failed (skips don't fail the run), non-zero
otherwise, with a per-check log line and a failure summary at the end.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `LITELLM_URL` | `http://litellm.local.ro` | gateway base URL |
| `OPEN_WEBUI_URL` | `https://open-webui.local.ro` | chat UI |
| `GRAFANA_URL` | `http://grafana.local.ro` | Grafana |
| `JAEGER_URL` | `http://jaeger.local.ro` | Jaeger |
| `LANGFUSE_URL` | `http://langfuse.local.ro` | Langfuse |
| `LITELLM_KEY` | `sk-litellm-master-change-me` | LiteLLM bearer token |
| `GRAFANA_USER` / `GRAFANA_PASSWORD` | `admin` / *(auto via kubectl)* | Grafana basic auth for the Prometheus check |
| `KUBECTL` | `microk8s kubectl` | kubectl command for the cluster tier |
| `TIMEOUT` | `120` | per-request timeout (seconds) |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-...0001` | Langfuse API public key (for `test_observability.py`) |
| `LANGFUSE_SECRET_KEY` | `sk-lf-...0001` | Langfuse API secret key (for `test_observability.py`) |

Flags: `--no-cluster` (skip the kubectl tier, smoke only), `--only <token>` (repeatable
filter), `--junit <path>`.

## Retrieve the LiteLLM master key

```bash
microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}{{"\n"}}'
```

## Notes

- **CPU (llama.cpp) traces** stop at the LiteLLM→pod boundary (llama.cpp emits
  no OTLP yet). vLLM emits OTLP on every GPU request, so
  `integration/trace-pipeline` looks for the `vllm-inference` service in Jaeger.
  However, all three models (including CPU) produce LiteLLM-level traces that
  reach both Jaeger and Langfuse — verified by `test_observability.py`.
- **Tool param dropping**: Open WebUI sends `tools` and `tool_choice` params by
  default. All models are registered with `drop_params: true` and
  `additional_drop_params: ["tools", "tool_choice"]` so these are stripped at the
  LiteLLM layer before reaching the backend.

