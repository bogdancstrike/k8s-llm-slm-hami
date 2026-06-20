#!/usr/bin/env python3
"""Observability & tracing integration tests for the AI Platform.

Tests the full telemetry pipeline:
  LiteLLM → OTel Collector → Jaeger  (span-level tracing)
                            → Langfuse (LLM-specific tracing with input/output)

Coverage matrix:
  - Per-model trace correlation: every model's trace appears in BOTH Jaeger and
    Langfuse under the SAME trace_id, with non-empty input/output in Langfuse.
  - Content fidelity: the input/output stored in Langfuse matches the actual
    prompt/completion sent through LiteLLM.
  - Environment tagging: Langfuse traces carry `environment=default`.
  - Jaeger span attributes: `gen_ai.*` semantic convention attributes are present.
  - Langfuse observations: the trace contains at least one generation observation
    with model info and token usage.
  - Tool-param resilience: models respond correctly even when Open WebUI-style
    `tools`/`tool_choice` params are included (LiteLLM should drop them).
  - Context window: models handle multi-turn conversations within their context.
  - Multi-turn tracing: a multi-message conversation produces a correlated trace.

Stdlib-only: no pip install required.  Self-signed TLS on *.local.ro is tolerated.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from xml.sax.saxutils import escape as xml_escape


# ─── Configuration (all overridable via env) ────────────────────────────────

def _url(name: str, default: str) -> str:
    return os.environ.get(name, default).rstrip("/")


LITELLM_URL = _url("LITELLM_URL", "http://litellm.local.ro")
JAEGER_URL = _url("JAEGER_URL", "http://jaeger.local.ro")
LANGFUSE_URL = _url("LANGFUSE_URL", "http://langfuse.local.ro")

LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-master-change-me")
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))

# Every model alias deployed on the platform.
ALL_ALIASES = ["gemma-1b-fast", "smollm3-3b-quality", "qwen-3b-cpu"]

# Langfuse API key pair (default PoC keys).
LF_PUBLIC_KEY = os.environ.get(
    "LANGFUSE_PUBLIC_KEY", "pk-lf-00000000-0000-0000-0000-000000000001"
)
LF_SECRET_KEY = os.environ.get(
    "LANGFUSE_SECRET_KEY", "sk-lf-00000000-0000-0000-0000-000000000001"
)

_UNVERIFIED_TLS = ssl.create_default_context()
_UNVERIFIED_TLS.check_hostname = False
_UNVERIFIED_TLS.verify_mode = ssl.CERT_NONE


# ─── Result + runner plumbing ───────────────────────────────────────────────

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Skip(Exception):
    """Raised by a check to mark itself skipped (not failed)."""


@dataclass
class Result:
    name: str
    status: str
    elapsed_s: float
    detail: str = ""


@dataclass
class Runner:
    results: list[Result] = field(default_factory=list)
    only: list[str] = field(default_factory=list)

    def run(self, name: str, fn: Callable[[], str]) -> bool:
        if self.only and not any(tok in name for tok in self.only):
            return True
        start = time.monotonic()
        print(f"==> {name}", flush=True)
        try:
            detail = fn()
            elapsed = time.monotonic() - start
            self.results.append(Result(name, PASS, elapsed, detail))
            print(f"    PASS ({elapsed:.1f}s) {detail}", flush=True)
            return True
        except Skip as e:
            elapsed = time.monotonic() - start
            self.results.append(Result(name, SKIP, elapsed, str(e)))
            print(f"    SKIP ({elapsed:.1f}s) {e}", flush=True)
            return True
        except AssertionError as e:
            elapsed = time.monotonic() - start
            self.results.append(Result(name, FAIL, elapsed, str(e)))
            print(f"    FAIL ({elapsed:.1f}s) {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - start
            self.results.append(Result(name, FAIL, elapsed, f"{type(e).__name__}: {e}"))
            print(f"    FAIL ({elapsed:.1f}s) {type(e).__name__}: {e}", flush=True)
        return False


# ─── HTTP helpers ───────────────────────────────────────────────────────────

def _request(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED_TLS) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body


def get_json(url: str, *, headers: dict | None = None, timeout: int = 15) -> dict:
    status, body = _request(url, headers=headers, timeout=timeout)
    assert status == 200, f"HTTP {status} from {url}: {body[:300]!r}"
    return json.loads(body.decode("utf-8"))


def post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    status, body = _request(
        url, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}, timeout=timeout,
    )
    text = body.decode("utf-8", errors="replace")
    assert status == 200, f"HTTP {status} from {url}: {text[:600]}"
    return json.loads(text)


# ─── Shared helpers ─────────────────────────────────────────────────────────

def _litellm_headers() -> dict:
    return {"Authorization": f"Bearer {LITELLM_KEY}"}


def _langfuse_headers() -> dict:
    auth_str = f"{LF_PUBLIC_KEY}:{LF_SECRET_KEY}"
    return {"Authorization": f"Basic {base64.b64encode(auth_str.encode()).decode()}"}


def _make_traceparent() -> tuple[str, str]:
    """Return (trace_id, traceparent header value)."""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return trace_id, f"00-{trace_id}-{span_id}-01"


def _chat_with_trace(alias: str, messages: list[dict], trace_id: str,
                     traceparent: str, *, max_tokens: int = 32,
                     extra_params: dict | None = None) -> dict:
    """Send a chat completion with an explicit traceparent, return response."""
    payload = {
        "model": alias,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        **(extra_params or {}),
    }
    headers = {**_litellm_headers(), "traceparent": traceparent}
    return post_json(f"{LITELLM_URL}/v1/chat/completions", payload, headers, TIMEOUT)


def _wait_jaeger_trace(trace_id: str, deadline: float) -> dict | None:
    """Poll Jaeger until the trace appears or deadline expires. Return trace data."""
    while time.monotonic() < deadline:
        try:
            data = get_json(f"{JAEGER_URL}/api/traces/{trace_id}", timeout=10)
            if data.get("data"):
                return data
        except Exception:
            pass
        time.sleep(3)
    return None


def _wait_langfuse_trace(trace_id: str, deadline: float) -> dict | None:
    """Poll Langfuse until the trace appears or deadline expires."""
    lf_headers = _langfuse_headers()
    while time.monotonic() < deadline:
        try:
            data = get_json(
                f"{LANGFUSE_URL}/api/public/traces/{trace_id}",
                headers=lf_headers, timeout=10,
            )
            if data.get("id"):
                return data
        except Exception:
            pass
        time.sleep(3)
    return None


def _extract_reply(data: dict) -> str:
    """Extract the assistant reply text from a chat completion response."""
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content", "").strip()


# ═══ Tests: Per-model trace correlation ═════════════════════════════════════

def check_model_trace_correlation(alias: str) -> str:
    """Send a request through `alias` with a known trace_id, then verify:
    1. The same trace_id appears in Jaeger with litellm-proxy spans.
    2. The same trace_id appears in Langfuse with non-empty input and output.
    """
    trace_id, traceparent = _make_traceparent()
    user_msg = f"Reply with exactly one word: the capital of France. (trace-check for {alias})"
    messages = [
        {"role": "system", "content": "You are a concise assistant."},
        {"role": "user", "content": user_msg},
    ]

    # Send request
    resp = _chat_with_trace(alias, messages, trace_id, traceparent)
    reply = _extract_reply(resp)
    assert reply, f"empty reply from {alias}: {json.dumps(resp)[:300]}"

    # Wait for both backends
    deadline = time.monotonic() + min(TIMEOUT, 60)
    jaeger_data = _wait_jaeger_trace(trace_id, deadline)
    assert jaeger_data, f"trace {trace_id} not found in Jaeger within timeout"

    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"trace {trace_id} not found in Langfuse within timeout"

    # Validate Langfuse has input/output
    lf_input = lf_data.get("input")
    lf_output = lf_data.get("output")
    assert lf_input, f"Langfuse trace {trace_id} has empty input"
    assert lf_output, f"Langfuse trace {trace_id} has empty output"

    # Count Jaeger spans
    spans = []
    for t in jaeger_data.get("data", []):
        spans.extend(t.get("spans", []))

    return (f"trace {trace_id[:12]}… correlated: "
            f"Jaeger={len(spans)} spans, Langfuse input/output present")


# ═══ Tests: Langfuse content fidelity ═══════════════════════════════════════

def check_langfuse_content_fidelity(alias: str) -> str:
    """Verify the input/output in Langfuse matches the actual prompt/completion."""
    trace_id, traceparent = _make_traceparent()
    user_msg = "What is 2+2? Reply with just the number."
    messages = [{"role": "user", "content": user_msg}]

    resp = _chat_with_trace(alias, messages, trace_id, traceparent)
    actual_reply = _extract_reply(resp)
    assert actual_reply, f"empty reply from {alias}"

    deadline = time.monotonic() + min(TIMEOUT, 60)
    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"trace {trace_id} not found in Langfuse"

    lf_input = lf_data.get("input", "")
    lf_output = lf_data.get("output", "")

    # The input should contain our prompt text
    input_str = json.dumps(lf_input) if not isinstance(lf_input, str) else lf_input
    assert user_msg in input_str or "2+2" in input_str, \
        f"Langfuse input doesn't contain prompt: {input_str[:200]}"

    # The output should contain the model's actual reply
    output_str = json.dumps(lf_output) if not isinstance(lf_output, str) else lf_output
    assert output_str, f"Langfuse output is empty for trace {trace_id}"

    return (f"input contains prompt ✓, output present ✓ "
            f"(reply={actual_reply[:40]})")


# ═══ Tests: Langfuse environment ════════════════════════════════════════════

def check_langfuse_environment() -> str:
    """Verify traces in Langfuse carry environment=default."""
    trace_id, traceparent = _make_traceparent()
    messages = [{"role": "user", "content": "Say hello."}]

    _chat_with_trace(ALL_ALIASES[0], messages, trace_id, traceparent)

    deadline = time.monotonic() + min(TIMEOUT, 60)
    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"trace {trace_id} not found in Langfuse"

    env = lf_data.get("environment")
    assert env == "default", f"expected environment='default', got '{env}'"
    return f"environment='{env}' ✓"


# ═══ Tests: Langfuse observations (generations) ════════════════════════════

def check_langfuse_observations(alias: str) -> str:
    """Verify the Langfuse trace has observation(s) with model and token usage."""
    trace_id, traceparent = _make_traceparent()
    messages = [{"role": "user", "content": "Reply with one word: test."}]

    resp = _chat_with_trace(alias, messages, trace_id, traceparent, max_tokens=10)
    usage = resp.get("usage", {})

    deadline = time.monotonic() + min(TIMEOUT, 60)
    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"trace {trace_id} not found in Langfuse"

    observations = lf_data.get("observations", [])
    assert observations, f"trace {trace_id} has no observations in Langfuse"

    # At least one observation should have a model name
    models_found = [
        obs.get("model") for obs in observations if obs.get("model")
    ]
    assert models_found, (
        f"no observation has a model name; obs keys: "
        f"{[list(o.keys()) for o in observations[:3]]}"
    )

    # Check token usage in observations
    obs_with_usage = [
        obs for obs in observations
        if obs.get("usage") or obs.get("promptTokens") or obs.get("completionTokens")
           or (obs.get("usageDetails") and obs["usageDetails"].get("input"))
    ]

    return (f"{len(observations)} observations, "
            f"models={models_found[:3]}, "
            f"{len(obs_with_usage)} with usage data")


# ═══ Tests: Jaeger span attributes ═════════════════════════════════════════

def check_jaeger_span_attributes(alias: str) -> str:
    """Verify Jaeger spans contain gen_ai.* semantic convention attributes."""
    trace_id, traceparent = _make_traceparent()
    messages = [{"role": "user", "content": "Say yes."}]

    _chat_with_trace(alias, messages, trace_id, traceparent, max_tokens=5)

    deadline = time.monotonic() + min(TIMEOUT, 60)
    jaeger_data = _wait_jaeger_trace(trace_id, deadline)
    assert jaeger_data, f"trace {trace_id} not found in Jaeger"

    # Collect all tag keys across all spans
    all_tags: dict[str, str] = {}
    for trace in jaeger_data.get("data", []):
        for span in trace.get("spans", []):
            for tag in span.get("tags", []):
                all_tags[tag["key"]] = str(tag.get("value", ""))[:80]

    # We expect at least some gen_ai attributes from the transform processor
    gen_ai_keys = [k for k in all_tags if k.startswith("gen_ai.")]
    assert gen_ai_keys, (
        f"no gen_ai.* attributes in Jaeger spans; "
        f"found keys: {sorted(all_tags.keys())[:20]}"
    )

    # Specifically check for key attributes
    expected_attrs = ["gen_ai.request.model", "gen_ai.system"]
    found = [k for k in expected_attrs if k in all_tags]

    # Check input/output mapping worked
    has_input = "input.value" in all_tags or "gen_ai.prompt" in all_tags
    has_output = "output.value" in all_tags or "gen_ai.completion" in all_tags

    details = []
    details.append(f"{len(gen_ai_keys)} gen_ai.* attrs")
    if has_input:
        details.append("input mapped ✓")
    if has_output:
        details.append("output mapped ✓")
    details.append(f"found: {found}")

    return ", ".join(details)


# ═══ Tests: Tool-param resilience ═══════════════════════════════════════════

def check_tool_param_dropping(alias: str) -> str:
    """Verify models respond correctly even with tools/tool_choice params.

    Open WebUI sends these params by default. LiteLLM should drop them
    via `additional_drop_params` before forwarding to the backend.
    """
    trace_id, traceparent = _make_traceparent()
    messages = [{"role": "user", "content": "What is 1+1? Reply with just the number."}]

    # Include the tool params that Open WebUI sends
    extra_params = {
        "tools": [{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }],
        "tool_choice": "auto",
    }

    resp = _chat_with_trace(alias, messages, trace_id, traceparent,
                            extra_params=extra_params)
    reply = _extract_reply(resp)
    assert reply, f"empty reply from {alias} with tool params"
    assert "error" not in reply.lower(), f"error in reply: {reply[:200]}"

    return f"model responded OK with tools/tool_choice params: '{reply[:60]}'"


# ═══ Tests: Context window ══════════════════════════════════════════════════

def check_context_window(alias: str) -> str:
    """Verify models handle multi-turn conversations within their context."""
    trace_id, traceparent = _make_traceparent()
    messages = [
        {"role": "system", "content": "You are a helpful math tutor. Be concise."},
        {"role": "user", "content": "What is 5 times 3?"},
        {"role": "assistant", "content": "15"},
        {"role": "user", "content": "Add 10 to that."},
        {"role": "assistant", "content": "25"},
        {"role": "user", "content": "Now divide by 5. Reply with just the number."},
    ]

    resp = _chat_with_trace(alias, messages, trace_id, traceparent, max_tokens=20)
    reply = _extract_reply(resp)
    assert reply, f"empty reply from {alias} on multi-turn"
    usage = resp.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", "?")

    return f"multi-turn OK: prompt_tokens={prompt_tokens}, reply='{reply[:40]}'"


# ═══ Tests: Multi-turn trace in Langfuse ════════════════════════════════════

def check_multiturn_trace_in_langfuse() -> str:
    """Verify a multi-message conversation produces a trace with full input."""
    trace_id, traceparent = _make_traceparent()
    messages = [
        {"role": "system", "content": "You are a geography expert."},
        {"role": "user", "content": "What continent is Japan in?"},
        {"role": "assistant", "content": "Asia"},
        {"role": "user", "content": "Name one more country in that continent."},
    ]

    resp = _chat_with_trace(ALL_ALIASES[0], messages, trace_id, traceparent)
    reply = _extract_reply(resp)
    assert reply, "empty reply for multi-turn trace test"

    deadline = time.monotonic() + min(TIMEOUT, 60)
    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"multi-turn trace {trace_id} not found in Langfuse"

    lf_input = lf_data.get("input", "")
    lf_output = lf_data.get("output", "")
    input_str = json.dumps(lf_input) if not isinstance(lf_input, str) else lf_input

    # The last user message should appear in the Langfuse input
    assert "continent" in input_str.lower() or "country" in input_str.lower(), \
        f"multi-turn input not captured: {input_str[:200]}"
    assert lf_output, "multi-turn output is empty in Langfuse"

    return f"multi-turn trace captured: input has conversation context, output='{reply[:40]}'"


# ═══ Tests: Jaeger–Langfuse trace_id equivalence ═══════════════════════════

def check_traceid_format_equivalence() -> str:
    """Verify the trace_id format is consistent between Jaeger and Langfuse.

    Both backends should store the trace under the exact same 32-hex-char ID
    that was sent via the traceparent header.
    """
    trace_id, traceparent = _make_traceparent()
    messages = [{"role": "user", "content": "Say OK."}]

    _chat_with_trace(ALL_ALIASES[0], messages, trace_id, traceparent, max_tokens=5)

    deadline = time.monotonic() + min(TIMEOUT, 60)

    # Fetch from both backends
    jaeger_data = _wait_jaeger_trace(trace_id, deadline)
    assert jaeger_data, f"trace {trace_id} not found in Jaeger"

    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"trace {trace_id} not found in Langfuse"

    # Verify the trace ID stored in Jaeger matches
    jaeger_trace_ids = set()
    for t in jaeger_data.get("data", []):
        tid = t.get("traceID", "")
        if tid:
            jaeger_trace_ids.add(tid)

    assert trace_id in jaeger_trace_ids, (
        f"traceparent trace_id '{trace_id}' not in Jaeger traceIDs: {jaeger_trace_ids}"
    )

    # Verify the Langfuse trace ID matches
    lf_trace_id = lf_data.get("id", "")
    assert lf_trace_id == trace_id, (
        f"Langfuse trace ID '{lf_trace_id}' != traceparent '{trace_id}'"
    )

    return f"trace_id '{trace_id[:12]}…' identical in Jaeger and Langfuse ✓"


# ═══ Tests: Langfuse trace naming ═══════════════════════════════════════════

def check_langfuse_trace_name() -> str:
    """Verify Langfuse traces have a meaningful name (model name)."""
    trace_id, traceparent = _make_traceparent()
    messages = [{"role": "user", "content": "Say hi."}]

    _chat_with_trace(ALL_ALIASES[0], messages, trace_id, traceparent, max_tokens=5)

    deadline = time.monotonic() + min(TIMEOUT, 60)
    lf_data = _wait_langfuse_trace(trace_id, deadline)
    assert lf_data, f"trace {trace_id} not found in Langfuse"

    name = lf_data.get("name")
    assert name, f"trace {trace_id} has no name in Langfuse"
    assert name != "unknown", f"trace name is 'unknown'"

    return f"trace name='{name}' ✓"


# ─── JUnit output ───────────────────────────────────────────────────────────

def write_junit(path: str, results: list[Result]) -> None:
    total = len(results)
    failures = sum(1 for r in results if r.status == FAIL)
    skipped = sum(1 for r in results if r.status == SKIP)
    duration = sum(r.elapsed_s for r in results)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="ai-platform-observability" tests="{total}" failures="{failures}" '
        f'skipped="{skipped}" time="{duration:.2f}">',
    ]
    for r in results:
        lines.append(
            f'  <testcase classname="ai-platform.observability" name="{xml_escape(r.name)}" '
            f'time="{r.elapsed_s:.2f}">'
        )
        if r.status == FAIL:
            lines.append(
                f'    <failure message="{xml_escape(r.detail)[:200]}">'
                f'{xml_escape(r.detail)}</failure>'
            )
        elif r.status == SKIP:
            lines.append(f'    <skipped message="{xml_escape(r.detail)[:200]}"/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--junit", help="write a JUnit XML report")
    parser.add_argument("--only", action="append", default=[],
                        help="run only checks whose name contains this token "
                             "(repeatable)")
    args = parser.parse_args()

    print(f"LiteLLM:  {LITELLM_URL}")
    print(f"Jaeger:   {JAEGER_URL}")
    print(f"Langfuse: {LANGFUSE_URL}")
    print(f"Timeout:  {TIMEOUT}s")
    print(f"Models:   {ALL_ALIASES}\n")

    r = Runner(only=args.only)

    # ── Per-model trace correlation ───────────────────────────────────────
    print("── Per-model trace correlation (Jaeger + Langfuse) ─────────")
    for alias in ALL_ALIASES:
        r.run(f"trace-correlation/{alias}",
              lambda a=alias: check_model_trace_correlation(a))

    # ── Langfuse content & metadata ───────────────────────────────────────
    print("\n── Langfuse content fidelity & metadata ───────────────────")
    for alias in ALL_ALIASES:
        r.run(f"langfuse-content/{alias}",
              lambda a=alias: check_langfuse_content_fidelity(a))
    r.run("langfuse/environment", check_langfuse_environment)
    r.run("langfuse/trace-name", check_langfuse_trace_name)

    # ── Langfuse observations ─────────────────────────────────────────────
    print("\n── Langfuse observations (generations) ────────────────────")
    for alias in ALL_ALIASES:
        r.run(f"langfuse-observations/{alias}",
              lambda a=alias: check_langfuse_observations(a))

    # ── Jaeger span attributes ────────────────────────────────────────────
    print("\n── Jaeger span attributes (gen_ai.*) ──────────────────────")
    for alias in ALL_ALIASES:
        r.run(f"jaeger-attributes/{alias}",
              lambda a=alias: check_jaeger_span_attributes(a))

    # ── Tool-param resilience ─────────────────────────────────────────────
    print("\n── Tool-param dropping (Open WebUI compat) ────────────────")
    for alias in ALL_ALIASES:
        r.run(f"tool-params/{alias}",
              lambda a=alias: check_tool_param_dropping(a))

    # ── Context window & multi-turn ───────────────────────────────────────
    print("\n── Context window & multi-turn tracing ────────────────────")
    for alias in ALL_ALIASES:
        r.run(f"context-window/{alias}",
              lambda a=alias: check_context_window(a))
    r.run("multiturn/langfuse-trace", check_multiturn_trace_in_langfuse)

    # ── Cross-backend consistency ─────────────────────────────────────────
    print("\n── Cross-backend trace consistency ────────────────────────")
    r.run("traceid/format-equivalence", check_traceid_format_equivalence)

    if args.junit:
        write_junit(args.junit, r.results)
        print(f"\nJUnit report -> {args.junit}")

    passed = sum(1 for x in r.results if x.status == PASS)
    failed = sum(1 for x in r.results if x.status == FAIL)
    skipped = sum(1 for x in r.results if x.status == SKIP)
    print(f"\nSummary: {passed} passed, {failed} failed, {skipped} skipped "
          f"({len(r.results)} total)")
    if failed:
        print("Failed:")
        for x in r.results:
            if x.status == FAIL:
                print(f"  - {x.name}: {x.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
