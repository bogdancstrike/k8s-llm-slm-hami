# tests/

End-to-end smoke tests for the QSINT AI Platform.

## What it covers

1. **LiteLLM gateway functional** — sends a `POST /v1/chat/completions` to
   `http://litellm.local.ro` for each of the three registered model aliases
   and asserts a non-empty assistant response is returned.
2. **Open WebUI reachability** — verifies `https://open-webui.local.ro/` (or
   the http variant) returns HTTP 2xx/3xx so the chat surface is live.

The three model aliases under test are:

| Alias                  | Backend                  | Where it runs |
|------------------------|--------------------------|---------------|
| `gemma-1b-fast`        | KServe + vLLM (GPU/HAMi) | TinyLlama 1.1B AWQ |
| `smollm3-3b-quality`   | KServe + vLLM (GPU/HAMi) | Qwen2.5 0.5B AWQ   |
| `qwen-3b-cpu`          | KServe + llama.cpp (CPU) | Qwen2.5 3B GGUF q4_k_m |

## Run

The tests use only the Python 3 standard library — no pip install needed.

```bash
# Default endpoints (host /etc/hosts must map *.local.ro → 127.0.0.1)
LITELLM_KEY="sk-litellm-master-change-me" \
  python3 tests/e2e_smoke.py

# Custom endpoints
LITELLM_URL=http://litellm.local.ro \
OPEN_WEBUI_URL=https://open-webui.local.ro \
LITELLM_KEY=sk-litellm-master-change-me \
TIMEOUT=120 \
  python3 tests/e2e_smoke.py
```

Exit code is `0` on success, non-zero on the first failure (with a per-step
log line). Use `--junit out.xml` to also emit a JUnit XML report.

## Retrieve the LiteLLM master key

```bash
microk8s kubectl -n ai-platform get secret litellm-secrets \
  -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}{{"\n"}}'
```
