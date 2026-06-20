# tests/

End-to-end **and integration** tests for the AI Platform. The suite
exercises *every component* — control plane, gateway, each deployed model, and
all four UIs — using only the Python 3 standard library (no `pip install`).

## What it covers

The suite runs in two tiers.

### HTTP / e2e tier (no cluster access needed)

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

### Cluster / integration tier (needs `kubectl`; auto-skips otherwise)

| Check | Asserts |
|---|---|
| `cluster/ready[<name>]` | `postgresql`, `litellm`, `langfuse-web`, `open-webui`, `jaeger`, `otel-collector` are Ready |
| `cluster/serving-runtimes` | `vllm-runtime` + `llamacpp-runtime` ClusterServingRuntimes exist |
| `cluster/isvc-ready[<model>]` | Each model's KServe InferenceService is `Ready` |
| `cluster/register-job[<model>]` | Each `<model>-litellm-register` Job Succeeded |
| `cluster/hami-scheduler` | HAMi scheduler pod is Running |
| `cluster/gpu-pods-on-hami` | GPU model pods are scheduled by `hami-scheduler` |
| `integration/prometheus-targets` | Via the Grafana Prometheus datasource proxy, `up` shows targets scraping — proves the Prometheus + Grafana metrics pipeline |

The models under test:

| Alias | Backend | Model |
|---|---|---|
| `gemma-1b-fast` | KServe + vLLM (GPU/HAMi) | TinyLlama 1.1B AWQ |
| `smollm3-3b-quality` | KServe + vLLM (GPU/HAMi) | Qwen2.5 0.5B AWQ |
| `qwen-3b-cpu` | KServe + llama.cpp (CPU) | Qwen2.5 3B GGUF q4_k_m |

## Run

```bash
# Full suite (HTTP + cluster tiers). Host /etc/hosts must map *.local.ro → 127.0.0.1.
LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')" \
python3 tests/e2e_smoke.py

# HTTP tier only (e.g. from a workstation without kubectl)
python3 tests/e2e_smoke.py --no-cluster

# Run a subset and emit a JUnit report
python3 tests/e2e_smoke.py --only litellm --only grafana --junit out.xml
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

Flags: `--no-cluster` (skip the kubectl tier), `--only <token>` (repeatable
filter), `--junit <path>`.

## Retrieve the LiteLLM master key

```bash
microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}{{"\n"}}'
```

## Notes

- **Langfuse** is checked for liveness only. The LiteLLM→Langfuse callback is
  inactive until project API keys are populated into `litellm-secrets`
  (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`), so ingestion isn't asserted
  end-to-end by default.
- **CPU (llama.cpp) traces** stop at the LiteLLM→pod boundary (llama.cpp emits
  no OTLP yet). vLLM emits OTLP on every GPU request, so
  `integration/trace-pipeline` looks for the `vllm-inference` service in Jaeger.
