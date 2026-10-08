"""
Checks for failure classification + retry (requirement 5) and finish() validation.
Tavily responses are simulated with httpx.MockTransport, and sleeping is faked,
so this runs in about a second and uses no credits. One case calls the real
Tavily API with a bogus key (free: rejected before any search runs).

Usage (from the repo root, tracker venv):
    .venv\Scripts\python.exe tests\failure_check.py
"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.config import load_config  # noqa: E402
from tracker.errors import (RetriesExhausted, RetryPolicy, TerminalError, ToolInputError,  # noqa: E402
                            call_with_retry)
from tracker.report import validate_report  # noqa: E402
from tracker.search import tavily_search  # noqa: E402
from tracker.urls import canonicalize  # noqa: E402

CFG = load_config()
POLICY = RetryPolicy.from_config(CFG["retry"])
OK_BODY = {"results": [{"title": "Qwen 4 released", "url": "https://example.com/q4", "content": "...", "score": 0.9}],
           "usage": {"credits": 1}}
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))


def scripted(*responses):
    """A fake Tavily: returns the given responses in order (an Exception is raised instead)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        item = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        return httpx.Response(status, json=body, headers=headers or {})
    return httpx.MockTransport(handler), calls


def run(*responses, query="open-weight model release"):
    transport, calls = scripted(*responses)
    sleeps: list[float] = []
    try:
        result = call_with_retry(
            lambda: tavily_search(query, api_key="test", cfg=CFG["search"], transport=transport),
            POLICY, what="search_web", sleep=sleeps.append)
        return result, None, calls["n"], sleeps
    except Exception as e:  # noqa: BLE001
        return None, e, calls["n"], sleeps


print("\n== Transient failures are retried with backoff, up to a cap")
r, e, n, s = run((503, {"detail": "busy"}, None), (200, OK_BODY, None))
check("503 then 200 -> success on attempt 2", r is not None and n == 2 and len(s) == 1, f"{e} n={n}")
r, e, n, s = run((429, {"detail": "slow down"}, {"retry-after": "7"}), (200, OK_BODY, None))
check("429 with Retry-After: 7 -> waits >= 7s, then succeeds", r is not None and s and s[0] >= 7, f"{e} sleeps={s}")
r, e, n, s = run(httpx.ConnectError("network is unreachable"))
check(f"network down -> {POLICY.max_attempts} attempts, then RetriesExhausted",
      isinstance(e, RetriesExhausted) and n == POLICY.max_attempts and len(s) == POLICY.max_attempts - 1, f"{e!r} n={n}")
check("backoff grows (1s, 2s, 4s, ... + jitter)",
      len(s) == POLICY.max_attempts - 1 and all(a < b for a, b in zip(s, s[1:])), f"sleeps={s}")
r, e, n, s = run(httpx.ReadTimeout("timed out"))
check("timeout -> retried, then RetriesExhausted", isinstance(e, RetriesExhausted) and n == POLICY.max_attempts, repr(e))
r, e, n, s = run((429, {"detail": "slow down"}, None))
check("429 that never clears -> RetriesExhausted (capped, not forever)",
      isinstance(e, RetriesExhausted) and n == POLICY.max_attempts, repr(e))

print("\n== Terminal failures stop immediately (no retry)")
for status, label in [(401, "bad API key"), (403, "forbidden"), (432, "monthly plan limit"), (433, "pay-as-you-go limit")]:
    r, e, n, s = run((status, {"detail": {"error": label}}, None))
    check(f"{status} {label} -> TerminalError after 1 attempt, no sleep",
          isinstance(e, TerminalError) and not isinstance(e, RetriesExhausted) and n == 1 and not s, f"{e!r} n={n}")
r, e, n, s = run((429, {"detail": "quota"}, {"retry-after": "3600"}))
check("429 asking to wait an hour -> treated as a quota: TerminalError, no sleep",
      isinstance(e, TerminalError) and n == 1 and not s, f"{e!r} n={n} sleeps={s}")

print("\n== Bad input from the model is reported back, not retried, not fatal")
r, e, n, s = run((400, {"detail": {"error": "Invalid topic"}}, None))
check("400 -> ToolInputError after 1 attempt", isinstance(e, ToolInputError) and n == 1, repr(e))
r, e, n, s = run((200, OK_BODY, None), query="   ")
check("empty query -> ToolInputError, no request sent", isinstance(e, ToolInputError) and n == 0, repr(e))

print("\n== Real Tavily, bogus key (costs nothing)")
try:
    tavily_search("test", api_key="tvly-this-key-is-bogus", cfg=CFG["search"])
    check("bogus key -> TerminalError", False, "request succeeded?!")
except TerminalError as e:
    check(f"bogus key -> TerminalError: {str(e)[:60]}", True)
except Exception as e:  # noqa: BLE001
    check("bogus key -> TerminalError", False, repr(e))

print("\n== finish(report) validation")
TEXT = "Alibaba released Qwen 4 today under the Apache 2.0 license. It has 72 billion parameters."
fetched = {canonicalize("https://example.com/q4"): TEXT}


def report(**over):
    dev = {"key": "qwen/qwen-4/release", "title": "Qwen 4 released", "summary": "Alibaba released Qwen 4.",
           "rank": 1, "sources": [{"url": "https://example.com/q4?utm_source=x",
                                   "evidence": "Alibaba released Qwen 4 today under the Apache 2.0 license."}]}
    dev.update(over)
    return {"developments": [dev]}


res = validate_report(report(), CFG["k"], fetched)
check("valid report with an exact quote is accepted", res.ok, str(res.problems))
res = validate_report(report(sources=[{"url": "https://example.com/q4",
                                       "evidence": "Qwen 4 has 400 billion parameters and beats GPT-6."}]), CFG["k"], fetched)
check("quote that is NOT in the article -> rejected (hallucination)", not res.ok and "not found" in str(res.problems),
      str(res.problems))
res = validate_report(report(sources=[{"url": "https://made-up.example/news",
                                       "evidence": "Alibaba released Qwen 4 today under the Apache 2.0 license."}]),
                      CFG["k"], fetched)
check("citing a URL that was never fetched -> rejected", not res.ok and "not fetched" in str(res.problems),
      str(res.problems))
res = validate_report(report(sources=[{"url": "javascript:alert(1)", "evidence": "x" * 30}]), CFG["k"], fetched)
check("javascript: source -> rejected", not res.ok, str(res.problems))
res = validate_report(report(rank=2), CFG["k"], fetched)
check("ranks with a gap (2 without 1) -> rejected", not res.ok, str(res.problems))
res = validate_report(report(key="Qwen 4!"), CFG["k"], fetched)
check("malformed key -> rejected", not res.ok, str(res.problems))
res = validate_report({"developments": [report()["developments"][0]] * (CFG["k"] + 1)}, CFG["k"], fetched)
check(f"more than k={CFG['k']} developments -> rejected", not res.ok, str(res.problems))
res = validate_report(report(sources=[{"url": "https://example.com/q4",
                                       "evidence": "alibaba  released Qwen 4 today under the Apache 2.0 license"}]),
                      CFG["k"], fetched)
check("quote matching ignores case/whitespace/trailing period", res.ok, str(res.problems))
no_title = report()
del no_title["developments"][0]["title"]
res = validate_report(no_title, CFG["k"], fetched)
check("missing title -> derived from the summary's first sentence", res.ok and
      res.report["developments"][0]["title"] == "Alibaba released Qwen 4.", str(res.problems or res.report))

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
