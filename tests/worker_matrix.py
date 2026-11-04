"""
The PDF's "Test it yourself" table (p.7), sent over real HTTP to a RUNNING worker.
Uses a few real model calls (tiny budgets), so run it on purpose, not in CI.

    backend\\.venv\\Scripts\\python.exe tests\\worker_matrix.py verifier   [--url https://<x>.onrender.com]
    backend\\.venv\\Scripts\\python.exe tests\\worker_matrix.py researcher [--url http://localhost:8001]

Tokens come from the environment / repo-root .env (RESEARCHER_TOKEN, VERIFIER_TOKEN); they are never
printed. "No new model calls or fetches on a replay" is checked in the worker's own trace: the
verifier's via its authenticated GET /trace, the researcher's via traces/researcher.jsonl.
Writes a JSON summary to traces/matrix-<worker>-<timestamp>.json.
"""
import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
results: list[dict] = []


def load_env() -> None:
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def check(name: str, cond: bool, detail: str = "", **data) -> None:
    results.append({"check": name, "ok": bool(cond), "detail": detail[:300], **data})
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail[:300]}"))


def envelope(payload, *, time_budget_ms=60000, budget=None):
    return {"request_id": str(uuid.uuid4()), "run_id": str(uuid.uuid4()), "time_budget_ms": time_budget_ms,
            "budget": budget or {"max_steps": 2, "max_fetches": 4, "max_tokens": 40000}, "payload": payload}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("worker", choices=["researcher", "verifier"])
    ap.add_argument("--url")
    args = ap.parse_args()
    load_env()
    w = args.worker
    url = (args.url or os.environ.get(f"{w.upper()}_URL")
           or {"researcher": "http://localhost:8001", "verifier": "http://localhost:8002"}[w]).rstrip("/")
    token = os.environ.get(f"{w.upper()}_TOKEN", "")
    path = "/research" if w == "researcher" else "/verify"
    good = {"Authorization": f"Bearer {token}"}
    client = httpx.Client(timeout=httpx.Timeout(240, connect=15))
    print(f"{w} at {url}")

    def post(body, headers=None, raw: bytes | None = None):
        t0 = time.monotonic()
        if raw is not None:
            r = client.post(url + path, content=raw, headers={**(headers or {}), "Content-Type": "application/json"})
        else:
            r = client.post(url + path, json=body, headers=headers or {})
        return r, int((time.monotonic() - t0) * 1000)

    def outbound_calls(request_id: str) -> list[dict] | None:
        if w == "verifier":
            try:
                lines = client.get(url + "/trace", headers=good).json()["lines"]
            except Exception:  # noqa: BLE001
                return None
        else:
            p = ROOT / "traces" / "researcher.jsonl"
            lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
        return [e for e in lines if e.get("request_id") == request_id and e.get("direction") == "out"]

    t0 = time.monotonic()
    r = client.get(url + "/healthz")
    check("GET /healthz without a token -> 200 {status: ok}", r.status_code == 200 and r.json() == {"status": "ok"},
          r.text, latency_ms=int((time.monotonic() - t0) * 1000))

    if w == "verifier":
        work = {"claims": [{"claim_id": "c1", "text": "The example.com domain is intended for use in documentation "
                            "or illustrative examples.", "source_url": "https://example.com/"}]}
    else:
        work = {"topic": "Open-weight AI model releases", "k": 3}

    body = envelope(work)
    r, _ = post(body)
    check("no token -> 401", r.status_code == 401, r.text)
    r, _ = post(body, {"Authorization": "Bearer wrong-token"})
    check("wrong token -> 401", r.status_code == 401, r.text)
    r, _ = post(None, {}, raw=b"{broken")
    check("no token + malformed body -> 401 (auth first)", r.status_code == 401, r.text)
    r, _ = post(None, good, raw=b"{broken")
    check("valid token + malformed JSON -> 400", r.status_code == 400, r.text)
    bad = dict(body, payload={"unexpected": True})
    r, _ = post(bad, good)
    check("valid token + body that fails the schema -> 400", r.status_code == 400, r.text)

    r1, ms1 = post(body, good)
    calls1 = outbound_calls(body["request_id"])
    shuffled = json.dumps({k: body[k] for k in reversed(list(body))}, indent=2).encode()
    r2, ms2 = post(None, good, raw=shuffled)
    calls2 = outbound_calls(body["request_id"])
    check("first request -> 200", r1.status_code == 200, r1.text, latency_ms=ms1, status=r1.json().get("status"))
    check("same request_id + same body (reordered keys) -> same status and body",
          r2.status_code == r1.status_code and r2.json() == r1.json(), r2.text, latency_ms=ms2)
    if calls1 is not None:
        check("the replay added no model calls or fetches in the worker's log",
              calls2 is not None and len(calls2) == len(calls1),
              f"{len(calls1)} outbound lines before, {len(calls2 or [])} after",
              outbound_before=len(calls1), outbound_after=len(calls2 or []))
    changed = dict(body, time_budget_ms=body["time_budget_ms"] - 1000)
    r, _ = post(changed, good)
    check("same request_id + different body (smaller time_budget_ms) -> 409", r.status_code == 409, r.text)

    # long enough that the duplicate surely arrives while the first is still running
    slow_work = work if w == "researcher" else {"claims": [
        {"claim_id": f"s{i}", "text": f"The {d} domain is reserved for documentation examples.",
         "source_url": f"https://{d}/"} for i, d in enumerate(["example.com", "example.org", "example.net"])]}
    slow = envelope(slow_work, budget={"max_steps": 4, "max_fetches": 6, "max_tokens": 60000})
    first: dict = {}
    th = threading.Thread(target=lambda: first.setdefault("r", post(slow, good)))
    th.start()
    time.sleep(0.4)
    r, _ = post(slow, good)
    th.join(300)
    check("same request_id while the first is still running -> 503 + Retry-After",
          r.status_code == 503 and r.headers.get("retry-after", "").isdigit(),
          f"{r.status_code} {r.text}", retry_after=r.headers.get("retry-after"))
    check("... and the first copy still completes normally", first.get("r") and first["r"][0].status_code == 200)

    if w == "verifier":
        evil = envelope({"claims": [
            {"claim_id": "meta", "text": "Instance metadata.", "source_url": "http://169.254.169.254/latest/meta-data/"},
            {"claim_id": "local", "text": "Local admin page.", "source_url": "http://localhost/"},
            {"claim_id": "v6", "text": "IPv6 loopback.", "source_url": "http://[::1]/"}]})
        r, ms = post(evil, good)
        out = r.json()
        check("claims at 169.254.169.254 / localhost / [::1] -> unreachable",
              r.status_code == 200 and [v["verdict"] for v in out["result"]["verdicts"]] == ["unreachable"] * 3,
              r.text, latency_ms=ms)
        check("... with no fetch attempted (usage.fetches == 0)", out["usage"]["fetches"] == 0, str(out["usage"]))
        calls = outbound_calls(evil["request_id"]) or []
        check("... and the worker's log shows 0 hops for them",
              all(e.get("hops") == 0 for e in calls if e["endpoint_or_tool"] == "fetch_article"),
              json.dumps([{k: e.get(k) for k in ("status", "hops", "detail")} for e in calls])[:300])

    tiny = envelope(work, time_budget_ms=1)
    r, ms = post(tiny, good)
    out = r.json()
    check("tiny time_budget_ms -> 200 partial, with what it had",
          r.status_code == 200 and out.get("status") == "partial"
          and out.get("error", {}).get("code") == "time_budget_exhausted", r.text, latency_ms=ms)

    ok = all(x["ok"] for x in results)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = ROOT / "traces" / f"matrix-{w}-{stamp}.json"
    out_path.write_text(json.dumps({"worker": w, "url": url, "at": stamp, "results": results}, indent=2),
                        encoding="utf-8")
    print(f"\n{sum(x['ok'] for x in results)} passed, {sum(not x['ok'] for x in results)} failed  ->  "
          f"{out_path.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
