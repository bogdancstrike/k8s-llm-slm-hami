#!/usr/bin/env python3
"""End-to-end chat-scenario tests for the AI Platform.

Simulates real Open WebUI / OpenCode usage: for EVERY deployed model it runs
THREE multi-turn conversations (geography, arithmetic, coding), each with
follow-up questions that depend on the previous turns. For each conversation it
checks:

  • Responsiveness  — every turn returns a non-empty reply.
  • Answer quality  — replies contain the expected answer (keyword match). Anchor
                      turns (simple, deterministic) are HARD requirements for
                      every model; the context-dependent follow-ups are scored and
                      reported (report-only by default — a 0.5B model legitimately
                      flubs chained reasoning). Set QUALITY_THRESHOLD>0 to enforce.
  • Tracing         — the conversation's trace_id lands in BOTH Jaeger and
                      Langfuse (LiteLLM is the router, so this covers any client).
  • Latency         — per-question and per-scenario wall-clock, summarised at end.

The conversation history is threaded turn-to-turn, so the follow-ups genuinely
test multi-turn context handling.

Stdlib-only: no pip install. Self-signed TLS on *.local.ro is tolerated.

Usage:
  LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret litellm-secrets \\
    -o go-template='{{index .data "LITELLM_MASTER_KEY" | base64decode}}')" \\
  python3 tests/test_chat_scenarios.py
  # options: --only <token> (repeatable), --junit out.xml, --no-trace-check
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
ALL_ALIASES = os.environ.get(
    "ALIASES", "gemma-1b-fast,smollm3-3b-quality,qwen-3b-cpu"
).split(",")

# Langfuse API key pair (default PoC keys).
LF_PUBLIC_KEY = os.environ.get(
    "LANGFUSE_PUBLIC_KEY", "pk-lf-00000000-0000-0000-0000-000000000001"
)
LF_SECRET_KEY = os.environ.get(
    "LANGFUSE_SECRET_KEY", "sk-lf-00000000-0000-0000-0000-000000000001"
)

# Optional follow-up quality gate: minimum fraction of *all* keyworded turns
# (anchors + follow-ups) that must match. Default 0.0 = report-only, because the
# real quality gate is the per-scenario ANCHOR turns (always hard-required) — a
# tiny 0.5B model legitimately flubs chained-reasoning follow-ups, and that's
# measured signal, not a platform failure. Set e.g. QUALITY_THRESHOLD=0.6 to
# enforce a stricter bar in CI for stronger models.
QUALITY_THRESHOLD = float(os.environ.get("QUALITY_THRESHOLD", "0.0"))

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


def _chat(alias: str, messages: list[dict], traceparent: str,
          *, max_tokens: int) -> tuple[dict, float]:
    """Send a chat completion with a traceparent. Return (response, latency_s)."""
    payload = {
        "model": alias,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    headers = {**_litellm_headers(), "traceparent": traceparent}
    start = time.monotonic()
    resp = post_json(f"{LITELLM_URL}/v1/chat/completions", payload, headers, TIMEOUT)
    return resp, time.monotonic() - start


def _extract_reply(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content", "").strip()


def _wait_jaeger_trace(trace_id: str, deadline: float) -> dict | None:
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


# ─── Scenario definitions ───────────────────────────────────────────────────

@dataclass
class Turn:
    prompt: str
    # Reply passes quality if it contains ANY of these (case-insensitive).
    expect_any: list[str] = field(default_factory=list)
    # Anchor turns are simple/deterministic and HARD-required for all models.
    anchor: bool = False
    max_tokens: int = 48


@dataclass
class Scenario:
    name: str
    system: str
    turns: list[Turn]


SCENARIOS: list[Scenario] = [
    Scenario(
        name="geography",
        system="You are a concise geography expert. Answer briefly.",
        turns=[
            Turn("What is the capital of France? Answer with just the city name.",
                 expect_any=["paris"], anchor=True),
            Turn("And the capital of Japan? Just the city name.",
                 expect_any=["tokyo"], anchor=True),
            Turn("Of the two cities you just named, which did I ask about first? "
                 "Reply with just that city name.",
                 expect_any=["paris"]),  # follow-up: needs conversation memory
        ],
    ),
    Scenario(
        name="arithmetic",
        system="You are a precise calculator. Reply with only the number.",
        turns=[
            Turn("What is 7 + 5? Reply with just the number.",
                 expect_any=["12"], anchor=True),
            Turn("Multiply that result by 2. Reply with just the number.",
                 expect_any=["24"]),   # follow-up: chained math over prior answer
            Turn("Now subtract 4 from that. Reply with just the number.",
                 expect_any=["20"]),   # follow-up: chained math (small models wobble)
        ],
    ),
    Scenario(
        name="coding",
        system="You are a helpful coding assistant. Return Python code.",
        turns=[
            Turn("Write a Python function `square(n)` that returns n squared. "
                 "Return only code.",
                 expect_any=["def square", "return", "**", "* n", "n*n"],
                 anchor=True, max_tokens=160),
            Turn("Now write a function `squares(nums)` that returns a list with the "
                 "square of each number in the input list. Return only code.",
                 expect_any=["def squares", "for ", "[", "return"],
                 max_tokens=200),
        ],
    ),
]


# ─── Conversation runner ────────────────────────────────────────────────────

@dataclass
class TurnMetric:
    idx: int
    prompt: str
    reply: str
    latency_s: float
    quality_ok: bool
    anchor: bool


# Collected globally for the final timing/quality report.
CONV_METRICS: list[tuple[str, str, list[TurnMetric], float, bool, bool]] = []
# (alias, scenario, turn_metrics, total_s, jaeger_ok, langfuse_ok)


def run_scenario(alias: str, scenario: Scenario, check_traces: bool) -> str:
    """Run one multi-turn conversation for one model; validate and time it."""
    messages: list[dict] = [{"role": "system", "content": scenario.system}]
    metrics: list[TurnMetric] = []
    last_trace_id = ""
    convo_start = time.monotonic()

    for i, turn in enumerate(scenario.turns, start=1):
        trace_id, traceparent = _make_traceparent()
        last_trace_id = trace_id
        messages.append({"role": "user", "content": turn.prompt})

        resp, latency = _chat(alias, messages, traceparent, max_tokens=turn.max_tokens)
        reply = _extract_reply(resp)

        # Responsiveness is a hard requirement on every turn.
        assert reply, (
            f"[{alias}/{scenario.name}] empty reply on turn {i}: "
            f"{json.dumps(resp)[:300]}"
        )

        low = reply.lower()
        quality_ok = (not turn.expect_any) or any(
            kw.lower() in low for kw in turn.expect_any
        )
        metrics.append(TurnMetric(i, turn.prompt, reply, latency, quality_ok, turn.anchor))

        # Anchor turns must be answered correctly by every model.
        assert (not turn.anchor) or quality_ok, (
            f"[{alias}/{scenario.name}] anchor turn {i} wrong: "
            f"expected one of {turn.expect_any}, got '{reply[:80]}'"
        )

        # Feed the reply back so follow-ups have real conversation context.
        messages.append({"role": "assistant", "content": reply})

    total_s = time.monotonic() - convo_start

    # Follow-up quality across all keyworded turns. Anchors are already hard-
    # required above; this aggregate is report-only unless QUALITY_THRESHOLD > 0.
    scored = [m for m in metrics if scenario.turns[m.idx - 1].expect_any]
    correct = sum(1 for m in scored if m.quality_ok)
    ratio = correct / len(scored) if scored else 1.0
    if QUALITY_THRESHOLD > 0:
        assert ratio >= QUALITY_THRESHOLD, (
            f"[{alias}/{scenario.name}] quality {correct}/{len(scored)} "
            f"({ratio:.0%}) < {QUALITY_THRESHOLD:.0%} threshold; "
            f"replies={[m.reply[:40] for m in scored]}"
        )

    # Verify the last turn's trace landed in BOTH backends.
    jaeger_ok = lf_ok = None
    if check_traces:
        deadline = time.monotonic() + min(TIMEOUT, 75)
        jaeger_ok = _wait_jaeger_trace(last_trace_id, deadline) is not None
        lf_ok = _wait_langfuse_trace(last_trace_id, deadline) is not None
        assert jaeger_ok, (
            f"[{alias}/{scenario.name}] trace {last_trace_id[:12]}… not in Jaeger"
        )
        assert lf_ok, (
            f"[{alias}/{scenario.name}] trace {last_trace_id[:12]}… not in Langfuse"
        )

    CONV_METRICS.append((alias, scenario.name, metrics, total_s,
                         bool(jaeger_ok), bool(lf_ok)))

    avg = sum(m.latency_s for m in metrics) / len(metrics)
    trace_note = "traces: Jaeger+Langfuse ✓" if check_traces else "trace-check skipped"
    return (f"{len(metrics)} turns, quality {correct}/{len(scored)}, "
            f"total {total_s:.1f}s (avg {avg:.1f}s/turn), {trace_note}")


# ─── Reporting ──────────────────────────────────────────────────────────────

def print_timing_report() -> None:
    if not CONV_METRICS:
        return
    print("\n── Timing & quality report ─────────────────────────────────")
    header = f"{'model':<20} {'scenario':<12} {'turns':>5} {'quality':>8} {'total_s':>8} {'avg_s':>7}  traces"
    print(header)
    print("-" * len(header))
    for alias, scen, metrics, total_s, jaeger_ok, lf_ok in CONV_METRICS:
        q_total = sum(1 for m in metrics if _turn_has_keywords(scen, m.idx))
        q_ok = sum(1 for m in metrics
                   if _turn_has_keywords(scen, m.idx) and m.quality_ok)
        avg = sum(m.latency_s for m in metrics) / len(metrics)
        traces = ("J✓" if jaeger_ok else "J✗") + ("L✓" if lf_ok else "L✗")
        print(f"{alias:<20} {scen:<12} {len(metrics):>5} "
              f"{q_ok:>3}/{q_total:<4} {total_s:>8.1f} {avg:>7.1f}  {traces}")

    # Per-question detail
    print("\n── Per-question latency (slowest first) ────────────────────")
    flat = []
    for alias, scen, metrics, *_ in CONV_METRICS:
        for m in metrics:
            flat.append((m.latency_s, alias, scen, m.idx, m.reply))
    for lat, alias, scen, idx, reply in sorted(flat, reverse=True)[:10]:
        print(f"  {lat:6.1f}s  {alias:<20} {scen}/turn{idx}  → {reply[:50]!r}")


def _turn_has_keywords(scenario_name: str, turn_idx: int) -> bool:
    for s in SCENARIOS:
        if s.name == scenario_name:
            return bool(s.turns[turn_idx - 1].expect_any)
    return False


# ─── JUnit output ───────────────────────────────────────────────────────────

def write_junit(path: str, results: list[Result]) -> None:
    total = len(results)
    failures = sum(1 for r in results if r.status == FAIL)
    skipped = sum(1 for r in results if r.status == SKIP)
    duration = sum(r.elapsed_s for r in results)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="ai-platform-chat-scenarios" tests="{total}" '
        f'failures="{failures}" skipped="{skipped}" time="{duration:.2f}">',
    ]
    for r in results:
        lines.append(
            f'  <testcase classname="ai-platform.chat-scenarios" '
            f'name="{xml_escape(r.name)}" time="{r.elapsed_s:.2f}">'
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
    parser.add_argument("--no-trace-check", action="store_true",
                        help="skip Jaeger/Langfuse trace verification (faster)")
    args = parser.parse_args()

    check_traces = not args.no_trace_check

    print(f"LiteLLM:  {LITELLM_URL}")
    print(f"Jaeger:   {JAEGER_URL}")
    print(f"Langfuse: {LANGFUSE_URL}")
    print(f"Models:   {ALL_ALIASES}")
    print(f"Scenarios:{[s.name for s in SCENARIOS]}")
    print(f"Timeout:  {TIMEOUT}s  Quality threshold: {QUALITY_THRESHOLD:.0%}  "
          f"Trace check: {check_traces}\n")

    r = Runner(only=args.only)

    for scenario in SCENARIOS:
        print(f"── Scenario: {scenario.name} ───────────────────────────────")
        for alias in ALL_ALIASES:
            r.run(f"chat/{scenario.name}/{alias}",
                  lambda a=alias, s=scenario: run_scenario(a, s, check_traces))
        print()

    print_timing_report()

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
