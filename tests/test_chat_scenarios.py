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
import html
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
import webbrowser
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

# Model aliases under test — populated at runtime from LiteLLM's /v1/models
# (see discover_aliases) so newly registered models are picked up out of the box.
# Override with ALIASES="a,b" to pin a subset.
ALL_ALIASES: list[str] = []

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


def discover_aliases() -> list[str]:
    """Resolve the model aliases to test.

    Dynamic by default: queries LiteLLM's `/v1/models` so any newly registered
    model is exercised automatically (the scenarios are model-agnostic). Set the
    ALIASES env var (comma-separated) to pin a specific subset instead.
    """
    override = os.environ.get("ALIASES", "").strip()
    if override:
        return [a.strip() for a in override.split(",") if a.strip()]
    data = get_json(f"{LITELLM_URL}/v1/models", headers=_litellm_headers(), timeout=15)
    ids = sorted(m["id"] for m in data.get("data", []) if m.get("id"))
    assert ids, f"{LITELLM_URL}/v1/models returned no models"
    return ids


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
    # Exact request that produced this turn (system + history + this user msg),
    # so the report can emit a runnable curl that reproduces it.
    request_messages: list = field(default_factory=list)
    max_tokens: int = 0


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
        metrics.append(TurnMetric(i, turn.prompt, reply, latency, quality_ok,
                                  turn.anchor, list(messages), turn.max_tokens))

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

def _oneline(s: str, width: int) -> str:
    """Collapse whitespace/newlines and truncate to `width` for table cells."""
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 1] + "…"


def print_qa_report() -> None:
    """Print each conversation as a question → answer table for easy review."""
    if not CONV_METRICS:
        return
    qw, aw = 46, 42
    print("\n── Conversation transcripts (question → answer) ────────────")
    for alias, scen, metrics, total_s, jaeger_ok, lf_ok in CONV_METRICS:
        traces = ("J✓" if jaeger_ok else "J✗") + ("L✓" if lf_ok else "L✗")
        print(f"\n▸ {alias} / {scen}   ({total_s:.1f}s total, traces {traces})")
        print(f"  {'#':>2}  {'question':<{qw}}  {'answer':<{aw}}  {'lat':>5}  ok")
        print("  " + "-" * (2 + 2 + qw + 2 + aw + 2 + 5 + 2 + 2))
        for m in metrics:
            keyworded = _turn_has_keywords(scen, m.idx)
            ok = ("✓" if m.quality_ok else "✗") if keyworded else "·"
            print(f"  {m.idx:>2}  {_oneline(m.prompt, qw):<{qw}}  "
                  f"{_oneline(m.reply, aw):<{aw}}  {m.latency_s:>4.1f}s  {ok}")


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


# ─── MDX document (embedded into the HTML report) ────────────────────────────

def _md_cell(s: str, width: int = 64) -> str:
    """Make a string safe for a Markdown/MDX table cell (one line, escaped)."""
    s = " ".join(s.split())
    if len(s) > width:
        s = s[: width - 1] + "…"
    # Escape table/MDX-significant chars. `{` and `<` would be parsed by MDX as
    # JS/JSX outside code fences; the faithful answer lives in a fenced block below.
    return (s.replace("\\", "")
             .replace("|", "\\|")
             .replace("`", "'")
             .replace("{", "(").replace("}", ")")
             .replace("<", "‹").replace(">", "›"))


def _curl_for(alias: str, messages: list[dict], max_tokens: int) -> str:
    """A copy-paste curl that reproduces one turn. Uses a quoted heredoc so the
    JSON body needs no escaping; `$LITELLM_KEY` in the header still expands."""
    payload = {
        "model": alias,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        f"curl -s {LITELLM_URL}/v1/chat/completions \\\n"
        f'  -H "Authorization: Bearer $LITELLM_KEY" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f"  -d @- <<'JSON' | jq -r '.choices[0].message.content'\n"
        f"{body}\n"
        f"JSON"
    )


def _code_block(content: str, lang: str = "") -> list[str]:
    """Fenced code-block lines using a fence longer than any backtick run inside,
    so code answers that themselves contain ``` don't terminate the fence early."""
    longest = cur = 0
    for ch in content:
        cur = cur + 1 if ch == "`" else 0
        longest = max(longest, cur)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{lang}", content, fence]


def build_mdx_text(results: list[Result], when: str, check_traces: bool) -> str:
    """Build the full MDX document — valid MDX: GFM tables + JSX <details> blocks,
    fenced code with safe fence lengths. Rendered by the .html via @mdx-js/mdx."""
    passed = sum(1 for x in results if x.status == PASS)
    failed = sum(1 for x in results if x.status == FAIL)
    skipped = sum(1 for x in results if x.status == SKIP)

    out: list[str] = [
        "---",
        "title: AI Platform — Chat Scenario Report",
        f"date: {when}",
        f"litellm: {LITELLM_URL}",
        f"models: [{', '.join(ALL_ALIASES)}]",
        f"result: {passed} passed, {failed} failed, {skipped} skipped",
        "---",
        "",
        "# AI Platform — Chat Scenario Report",
        "",
        f"_Generated {when} · LiteLLM `{LITELLM_URL}` · "
        f"trace check: {'on' if check_traces else 'off'}_",
        "",
        f"**Result: {passed} passed, {failed} failed, {skipped} skipped.**",
        "",
        "Export your key once, then any `curl` below re-runs that exact turn "
        "(full conversation history included) and prints the model's answer:",
        "",
        *_code_block(
            'export LITELLM_KEY="$(microk8s kubectl -n ai-platform get secret '
            "litellm-secrets -o go-template='{{index .data \"LITELLM_MASTER_KEY\" "
            "| base64decode}}')\"", "bash"),
        "",
        "## Summary",
        "",
        "| model | scenario | turns | quality | total (s) | avg/turn (s) | traces |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for alias, scen, metrics, total_s, jaeger_ok, lf_ok in CONV_METRICS:
        q_total = sum(1 for m in metrics if _turn_has_keywords(scen, m.idx))
        q_ok = sum(1 for m in metrics
                   if _turn_has_keywords(scen, m.idx) and m.quality_ok)
        avg = sum(m.latency_s for m in metrics) / len(metrics)
        traces = ("J✓" if jaeger_ok else "J✗") + ("L✓" if lf_ok else "L✗")
        out.append(f"| `{alias}` | {scen} | {len(metrics)} | {q_ok}/{q_total} | "
                   f"{total_s:.1f} | {avg:.2f} | {traces} |")

    out += ["", "## Transcripts", ""]
    for alias, scen, metrics, total_s, jaeger_ok, lf_ok in CONV_METRICS:
        traces = ("J✓" if jaeger_ok else "J✗") + ("L✓" if lf_ok else "L✗")
        out += [
            f"### `{alias}` — {scen}",
            "",
            f"_{total_s:.1f}s total · traces {traces}_",
            "",
            "| # | question | answer | lat (s) | ok |",
            "|---:|---|---|---:|:--:|",
        ]
        for m in metrics:
            keyworded = _turn_has_keywords(scen, m.idx)
            ok = ("✓" if m.quality_ok else "✗") if keyworded else "·"
            out.append(f"| {m.idx} | {_md_cell(m.prompt)} | {_md_cell(m.reply)} "
                       f"| {m.latency_s:.1f} | {ok} |")
        out.append("")
        # Per-turn full answer + runnable curl. Blank lines (no indentation) let
        # MDX parse the markdown children inside the <details> JSX element.
        for m in metrics:
            out += [
                "<details>",
                f"<summary>turn {m.idx} — full answer · re-run</summary>",
                "",
                "**Answer:**",
                "",
                *_code_block(m.reply, "text"),
                "",
                "**Re-run this turn:**",
                "",
                *_code_block(_curl_for(alias, m.request_messages, m.max_tokens), "bash"),
                "",
                "</details>",
                "",
            ]
    return "\n".join(out)




# ─── HTML report (single file; embeds the MDX, renders it via @mdx-js) ───────

_HTML_CSS = """
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  margin:2rem auto;max-width:1040px;padding:0 1rem;color:#1b1f23;line-height:1.5}
h1,h2,h3{line-height:1.25}h3{margin-top:1.6rem}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
th,td{border:1px solid #d0d7de;padding:.4rem .6rem;text-align:left;
  vertical-align:top;font-size:.92rem}
th{background:#f6f8fa}.num{text-align:right}.okcol{text-align:center;font-weight:700}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:.7rem;
  overflow:auto;font-size:.84rem}
code{background:#eff1f3;padding:.1rem .3rem;border-radius:4px}pre code{background:none;padding:0}
.meta{color:#57606a;font-size:.9rem}
.result{font-weight:700;padding:.35rem .7rem;border-radius:6px;display:inline-block}
.result.ok{background:#dafbe1;color:#1a7f37}.result.bad{background:#ffebe9;color:#cf222e}
details{border:1px solid #d0d7de;border-radius:6px;padding:.3rem .6rem;margin:.3rem 0}
summary{cursor:pointer;font-weight:600}
blockquote{color:#57606a;border-left:3px solid #d0d7de;margin:.5rem 0;padding:.1rem .8rem}
.pass{color:#1a7f37}.fail{color:#cf222e}.na{color:#8c959f}.err{color:#cf222e}
"""


def _h(s: str) -> str:
    return html.escape(str(s), quote=True)


# Vendored, self-contained MDX render bundle (see tests/vendor/README.md). Inlined
# into the report so it renders fully offline — no CDN. Exposes window.renderMDX.
_VENDOR_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "vendor", "mdx-bundle.js")

# Classic (non-module) bootstrap: the inlined IIFE sets window.renderMDX; compile
# + mount the embedded MDX, falling back to the raw source on any error.
_MDX_BOOTSTRAP_JS = """
(function () {
  var src = document.getElementById('mdx-source').textContent;
  var root = document.getElementById('root');
  function showRaw(err) {
    root.innerHTML = '<p class="err"><b>Could not render MDX:</b> ' +
      ((err && err.message) || err) + '</p>';
    var pre = document.createElement('pre'); pre.textContent = src;
    root.appendChild(pre);
  }
  try {
    var p = window.renderMDX(src, root);
    if (p && p.catch) p.catch(showRaw);
  } catch (err) { showRaw(err); }
})();
"""


def write_html_report(path: str, mdx_text: str, when: str) -> None:
    """Single self-contained, fully-offline HTML report: it embeds the full MDX
    document and the vendored @mdx-js render bundle, and renders the MDX in-browser.

    The MDX lives in a <script type="text/mdx"> tag (not fetched), so it works from
    a `file://` URL — the one HTML file *is* the renderable MDX, with no network.
    """
    # Only the literal `</script` can close an embedding <script> element early.
    embedded = mdx_text.replace("</script", "<\\/script")
    try:
        with open(_VENDOR_BUNDLE, encoding="utf-8") as f:
            bundle = f.read().replace("</script", "<\\/script")
        bundle_tag = f"<script>{bundle}</script>"
    except OSError:
        bundle_tag = ("<script>window.renderMDX=function(){"
                      "throw new Error('vendored mdx-bundle.js missing — see "
                      "tests/vendor/README.md')};</script>")
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>Chat Scenario Report — {_h(when)}</title>",
        f"<style>{_HTML_CSS}</style></head><body>",
        "<div id=root><p class=meta>Rendering MDX…</p></div>",
        '<script type="text/mdx" id="mdx-source">',
        embedded,
        "</script>",
        bundle_tag,
        f"<script>{_MDX_BOOTSTRAP_JS}</script>",
        "</body></html>",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


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
    parser.add_argument("--report-dir",
                        default=os.path.dirname(os.path.abspath(__file__)),
                        help="directory for the report_<timestamp>.html (default: "
                             "this script's dir)")
    parser.add_argument("--no-report", action="store_true",
                        help="don't write the HTML report")
    parser.add_argument("--no-open", action="store_true",
                        help="don't open the report in a browser when done")
    args = parser.parse_args()

    check_traces = not args.no_trace_check

    global ALL_ALIASES
    try:
        ALL_ALIASES = discover_aliases()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not discover models from {LITELLM_URL}/v1/models: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    src = "ALIASES env" if os.environ.get("ALIASES", "").strip() else "/v1/models"

    print(f"LiteLLM:  {LITELLM_URL}")
    print(f"Jaeger:   {JAEGER_URL}")
    print(f"Langfuse: {LANGFUSE_URL}")
    print(f"Models:   {ALL_ALIASES}  (from {src})")
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

    print_qa_report()
    print_timing_report()

    if not args.no_report:
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        html_path = os.path.join(args.report_dir, f"report_{stamp}.html")
        mdx_text = build_mdx_text(r.results, when, not args.no_trace_check)
        write_html_report(html_path, mdx_text, when)
        print(f"\nReport -> {html_path}  (HTML embedding MDX, rendered via @mdx-js/mdx)")
        # Open the report in the browser — it renders the embedded MDX.
        if not args.no_open:
            url = "file://" + urllib.request.pathname2url(os.path.abspath(html_path))
            try:
                if webbrowser.open(url):
                    print(f"Opened in browser: {url}")
                else:
                    print(f"Could not open a browser; view it at {url}")
            except Exception as e:  # noqa: BLE001
                print(f"Could not open a browser ({e}); view it at {url}")

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
