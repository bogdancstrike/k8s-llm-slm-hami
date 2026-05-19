#!/usr/bin/env python3
"""End-to-end smoke test for the QSINT AI Platform.

Sends a chat completion request through the LiteLLM gateway to each of the
three registered model aliases and verifies a non-empty response is produced.
Also confirms the Open WebUI surface is reachable.

Stdlib-only: no pip install required. Self-signed TLS on *.local.ro is
tolerated for the Open WebUI reachability check.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from xml.sax.saxutils import escape as xml_escape


LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm.local.ro").rstrip("/")
OPEN_WEBUI_URL = os.environ.get("OPEN_WEBUI_URL", "https://open-webui.local.ro").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-master-change-me")
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))

MODEL_ALIASES = [
    "gemma-1b-fast",
    "smollm3-3b-quality",
    "qwen-3b-cpu",
]

PROMPT = "Reply with exactly one short sentence: what is the capital of France?"


# Tolerate self-signed certs on *.local.ro for the smoke check. The functional
# LiteLLM call hits HTTP, so no TLS context is needed there.
_UNVERIFIED_TLS = ssl.create_default_context()
_UNVERIFIED_TLS.check_hostname = False
_UNVERIFIED_TLS.verify_mode = ssl.CERT_NONE


@dataclass
class Result:
    name: str
    passed: bool
    elapsed_s: float
    detail: str = ""


@dataclass
class Runner:
    results: list[Result] = field(default_factory=list)

    def run(self, name: str, fn: Callable[[], str]) -> bool:
        start = time.monotonic()
        print(f"==> {name}", flush=True)
        try:
            detail = fn()
            elapsed = time.monotonic() - start
            self.results.append(Result(name, True, elapsed, detail))
            print(f"    PASS ({elapsed:.1f}s) {detail}", flush=True)
            return True
        except AssertionError as e:
            elapsed = time.monotonic() - start
            self.results.append(Result(name, False, elapsed, str(e)))
            print(f"    FAIL ({elapsed:.1f}s) {e}", flush=True)
        except Exception as e:
            elapsed = time.monotonic() - start
            self.results.append(Result(name, False, elapsed, f"{type(e).__name__}: {e}"))
            print(f"    FAIL ({elapsed:.1f}s) {type(e).__name__}: {e}", flush=True)
        return False


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_litellm_healthy() -> str:
    url = f"{LITELLM_URL}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.status == 200, f"unexpected status {resp.status}"
            return f"liveliness OK at {url}"
    except urllib.error.HTTPError as e:
        # Some LiteLLM versions expose /health/readiness instead.
        if e.code == 404:
            url = f"{LITELLM_URL}/health/readiness"
            with urllib.request.urlopen(url, timeout=10) as resp:
                assert resp.status == 200, f"unexpected status {resp.status}"
                return f"readiness OK at {url}"
        raise


def check_models_registered() -> str:
    url = f"{LITELLM_URL}/v1/models"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {LITELLM_KEY}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ids = {m.get("id") for m in data.get("data", [])}
    missing = [m for m in MODEL_ALIASES if m not in ids]
    assert not missing, (
        f"missing model registrations in LiteLLM: {missing}. "
        f"Found: {sorted(ids)}"
    )
    return f"all 3 aliases registered ({sorted(MODEL_ALIASES)})"


def chat_via_litellm(alias: str) -> str:
    url = f"{LITELLM_URL}/v1/chat/completions"
    payload = {
        "model": alias,
        "messages": [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": PROMPT},
        ],
        "max_tokens": 64,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}
    data = _post_json(url, payload, headers, TIMEOUT)

    choices = data.get("choices") or []
    assert choices, f"no choices in response: {json.dumps(data)[:400]}"
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    assert content, f"empty assistant content: {json.dumps(data)[:400]}"

    usage = data.get("usage") or {}
    completion_tokens = usage.get("completion_tokens", "?")
    preview = content.replace("\n", " ")
    if len(preview) > 80:
        preview = preview[:77] + "..."
    return f'tokens={completion_tokens} reply="{preview}"'


def check_open_webui_reachable() -> str:
    candidates = [OPEN_WEBUI_URL, OPEN_WEBUI_URL.replace("https://", "http://", 1)]
    last_err: Exception | None = None
    for url in candidates:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10, context=_UNVERIFIED_TLS) as resp:
                assert 200 <= resp.status < 400, f"status {resp.status} from {url}"
                return f"reachable at {url} (status {resp.status})"
        except urllib.error.HTTPError as e:
            # 401/403 still means UI is up — gating is expected before signup.
            if 200 <= e.code < 500:
                return f"reachable at {url} (status {e.code})"
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise AssertionError(f"open-webui not reachable: {last_err}")


def write_junit(path: str, results: list[Result]) -> None:
    total = len(results)
    failures = sum(1 for r in results if not r.passed)
    duration = sum(r.elapsed_s for r in results)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="qsint-e2e-smoke" tests="{total}" failures="{failures}" time="{duration:.2f}">',
    ]
    for r in results:
        lines.append(
            f'  <testcase classname="qsint.e2e" name="{xml_escape(r.name)}" time="{r.elapsed_s:.2f}">'
        )
        if not r.passed:
            lines.append(
                f'    <failure message="{xml_escape(r.detail)[:200]}">{xml_escape(r.detail)}</failure>'
            )
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", help="path to write JUnit XML report")
    args = parser.parse_args()

    print(f"LiteLLM URL:    {LITELLM_URL}")
    print(f"Open WebUI URL: {OPEN_WEBUI_URL}")
    print(f"Timeout:        {TIMEOUT}s")
    print()

    runner = Runner()
    ok = True
    ok &= runner.run("litellm/health", check_litellm_healthy)
    ok &= runner.run("litellm/models-registered", check_models_registered)
    for alias in MODEL_ALIASES:
        ok &= runner.run(f"litellm/chat[{alias}]", lambda a=alias: chat_via_litellm(a))
    ok &= runner.run("open-webui/reachable", check_open_webui_reachable)

    if args.junit:
        write_junit(args.junit, runner.results)
        print(f"\nJUnit report -> {args.junit}")

    passed = sum(1 for r in runner.results if r.passed)
    total = len(runner.results)
    print(f"\nSummary: {passed}/{total} passed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
