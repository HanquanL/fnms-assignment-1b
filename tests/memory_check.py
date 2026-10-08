"""
Checks for memory between runs (requirements 6-7): key matching, new/still/
dropped, and a full save -> load -> second run -> reset cycle against the real
A1 backend, as a throwaway user (deleted at the end). No model or search calls.

Usage (backend running; from the repo root, tracker venv):
    .venv\\Scripts\\python.exe tests\\memory_check.py
    .venv\\Scripts\\python.exe tests\\memory_check.py --base-url http://localhost:8000
"""
import argparse
import copy
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.agent import AgentResult, Memory  # noqa: E402
from tracker.backend import BackendClient  # noqa: E402
from tracker.config import load_config  # noqa: E402
from tracker.errors import RetryPolicy, TerminalError  # noqa: E402
from tracker.memory import build_payload, classify, dedupe_against_memory, identity, memory_from_state  # noqa: E402
from tracker.render import render_markdown  # noqa: E402

CFG = load_config()
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))


def dev(key, rank, url, title=None):
    text = f"{title or key} was released under the Apache 2.0 license."
    ev = f"{title or key} is available today under the Apache 2.0 license."
    return {"key": key, "title": title or key, "rank": rank, "summary": text,
            "claims": [{"text": text, "url": url, "evidence": ev}], "sources": [{"url": url, "evidence": ev}]}


def result(devs, status="complete", articles=None, day=8):
    return AgentResult(status=status, stop_reason=None if status == "complete" else "budget: max_steps (25) reached",
                       report={"developments": devs, "notes": "", "evidence": []},
                       articles=articles if articles is not None else [
                           {"url": d["sources"][0]["url"] + "?utm_source=x", "canonical_url": d["sources"][0]["url"],
                            "title": d["title"], "status": "fetched", "reason": None, "http_status": 200,
                            "fetched_at": f"2026-10-{day:02d}T20:00:00+00:00"} for d in devs],
                       stats={"steps": 10, "searches": 3, "fetches": len(devs), "tokens_total": 1000,
                              "duration_s": 12.5},
                       started_at=f"2026-10-{day:02d}T20:00:00+00:00", finished_at=f"2026-10-{day:02d}T20:03:00+00:00",
                       model="gemini-test")


print("\n== Key matching (dedup safety net)")
check("'deepseek/v4.1-flash/release' ~ 'deepseek-ai/deepseek-v4-1-flash/launch'",
      identity("deepseek/v4.1-flash/release") == identity("deepseek-ai/deepseek-v4-1-flash/launch"))
check("'alibaba/qwen-4/release' ~ 'qwen/qwen4/weights'",
      identity("alibaba/qwen-4/release") == identity("qwen/qwen4/weights"))
check("different models stay different: qwen-4 vs qwen-4.5",
      identity("qwen/qwen-4/release") != identity("qwen/qwen-4.5/release"))
mem = Memory(developments=[{"key": "deepseek/v4.1-flash/release", "title": "t", "summary": "s", "sources": []}])
rep = {"developments": [dev("deepseek-ai/deepseek-v4-1-flash/launch", 1, "https://a.example/1"),
                        dev("google/gemma-5/release", 2, "https://a.example/2"),
                        dev("deepseek/v4-1-flash/benchmark", 3, "https://a.example/3")]}
renames = dedupe_against_memory(rep, mem)
keys = [d["key"] for d in rep["developments"]]
check("a near-miss key is renamed to the known key", keys[0] == "deepseek/v4.1-flash/release", str(keys))
check("two entries for the same development are merged (sources combined), ranks renumbered",
      keys == ["deepseek/v4.1-flash/release", "google/gemma-5/release"]
      and len(rep["developments"][0]["sources"]) == 2 and [d["rank"] for d in rep["developments"]] == [1, 2],
      f"{keys} {rep['developments'][0]['sources']}")
check("renames are reported for the trace",
      ("deepseek-ai/deepseek-v4-1-flash/launch", "deepseek/v4.1-flash/release") in renames
      and any("merged" in old for old, _ in renames), str(renames))

print("\n== new / still / dropped")
mem = Memory(last_top_k=[{"rank": 1, "key": "a/x/release", "title": "X", "summary": "sx"},
                         {"rank": 2, "key": "b/y/release", "title": "Y", "summary": "sy"}])
rep = {"developments": [dev("a/x/release", 1, "https://e.example/x"), dev("c/z/release", 2, "https://e.example/z")]}
changes, dropped = classify(rep, mem, run_complete=True)
check("in last top K -> still; not -> new", changes == {"a/x/release": "still", "c/z/release": "new"}, str(changes))
check("in last top K but not now -> dropped", [d["key"] for d in dropped] == ["b/y/release"], str(dropped))
changes, dropped = classify(rep, mem, run_complete=False)
check("a partial run drops nothing (it didn't look hard enough to say)", dropped == [], str(dropped))

print("\n== Report layout")
md1 = render_markdown(CFG, result([dev("a/x/release", 1, "https://e.example/x")]))
check("run 1: a plain top-K list", "## Top 5" in md1 and "New since last run" not in md1)
md2 = render_markdown(CFG, result(rep["developments"]), changes={"a/x/release": "still", "c/z/release": "new"},
                      dropped=[{"rank": 2, "key": "b/y/release", "title": "Y", "summary": "sy"}], run_label="run2")
order = [md2.find(h) for h in ("## New since last run", "## Still in top K", "## Dropped")]
check("run 2: New since last run, then Still in top K, then Dropped", all(i >= 0 for i in order) and order == sorted(order),
      str(order))

# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://localhost:8000")
base = parser.parse_args().base_url.rstrip("/")

print(f"\n== Full cycle against the backend at {base} (throwaway user)")
try:
    httpx.get(base + "/healthz", timeout=5)
except httpx.HTTPError:
    print(f"  SKIP  backend not reachable at {base}; start it and rerun for the end-to-end checks")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

uname, pw = f"trk_mem_{uuid.uuid4().hex[:8]}", "Password123!"
r = httpx.post(base + "/api/auth/register", json={"username": uname, "password": pw})
check("register throwaway user", r.status_code == 201, r.text[:200])
user_id = r.json().get("id")
retry = RetryPolicy(2, 0.1, 1)
api = BackendClient(base, uname, pw, retry)

state = api.load_state()
check("first load: empty memory", state["seen_urls"] == [] and state["last_run_id"] is None, str(state)[:200])

run1_devs = [dev("mistral/large-4/release", 1, "https://e.example/mistral", "Mistral Large 4"),
             dev("qwen/qwen-4/release", 2, "https://e.example/qwen", "Qwen 4"),
             dev("google/gemma-5/release", 3, "https://e.example/gemma", "Gemma 5")]
mem1 = memory_from_state(state)
res1 = result(copy.deepcopy(run1_devs))
ch1, dr1 = classify(res1.report, mem1, True)
saved = api.save_run(build_payload(CFG, res1, render_markdown(CFG, res1), ch1, dr1, res1.model))
check("run 1 saved: 3 new, 3 fetched", saved["counts"]["new"] == 3 and saved["counts"]["fetched"] == 3, str(saved)[:300])

mem2 = memory_from_state(api.load_state())
check("run 2 loads run 1's memory: 3 seen URLs, 3 developments, last top 3",
      len(mem2.seen_urls) == 3 and len(mem2.developments) == 3 and len(mem2.last_top_k) == 3,
      f"{len(mem2.seen_urls)} {len(mem2.developments)} {len(mem2.last_top_k)}")
check("seen URLs are canonical (tracking params gone)", "https://e.example/qwen" in mem2.seen_urls, str(mem2.seen_urls))

run2_devs = [dev("alibaba/qwen4/weights", 1, "https://other.example/qwen-news", "Qwen 4"),   # same dev, new URL+key
             dev("meta/llama-5/release", 2, "https://e.example/llama", "Llama 5"),          # brand new
             dev("mistral/large-4/release", 3, "https://e.example/mistral", "Mistral Large 4")]  # still
res2 = result(copy.deepcopy(run2_devs), day=9, articles=[
    {"url": "https://e.example/mistral", "canonical_url": "https://e.example/mistral", "title": None,
     "status": "skipped", "reason": "already fetched in an earlier run", "http_status": None,
     "fetched_at": "2026-10-09T20:00:00+00:00"},
    {"url": "https://other.example/qwen-news", "canonical_url": "https://other.example/qwen-news", "title": "Qwen 4 news",
     "status": "fetched", "reason": None, "http_status": 200, "fetched_at": "2026-10-09T20:01:00+00:00"},
    {"url": "http://127.0.0.1/", "canonical_url": "http://127.0.0.1/", "title": None, "status": "rejected",
     "reason": "host resolves to a loopback address", "http_status": None, "fetched_at": "2026-10-09T20:02:00+00:00"},
    {"url": "https://e.example/llama", "canonical_url": "https://e.example/llama", "title": "Llama 5",
     "status": "fetched", "reason": None, "http_status": 200, "fetched_at": "2026-10-09T20:03:00+00:00"}])
renames = dedupe_against_memory(res2.report, mem2)
check("the Qwen article under a new URL and key is recognized as the known development",
      ("alibaba/qwen4/weights", "qwen/qwen-4/release") in renames, str(renames))
ch2, dr2 = classify(res2.report, mem2, True)
check("run 2: qwen + mistral still, llama new, gemma dropped",
      ch2 == {"qwen/qwen-4/release": "still", "meta/llama-5/release": "new", "mistral/large-4/release": "still"}
      and [d["key"] for d in dr2] == ["google/gemma-5/release"], f"{ch2} {[d['key'] for d in dr2]}")
md2 = render_markdown(CFG, res2, changes=ch2, dropped=dr2, run_label="run2")
saved2 = api.save_run(build_payload(CFG, res2, md2, ch2, dr2, res2.model))
c = saved2["counts"]
check("run 2 saved: new 1, still 2, dropped 1, skipped 1, rejected 1",
      (c["new"], c["still"], c["dropped"], c["skipped"], c["rejected"]) == (1, 2, 1, 1, 1), str(c))

mem3 = memory_from_state(api.load_state())
qwen = next(d for d in mem3.developments if d["key"] == "qwen/qwen-4/release")
check("still ONE Qwen development, now with both sources",
      sum(d["key"].startswith("qwen/") for d in mem3.developments) == 1 and len(qwen["sources"]) == 2,
      str(qwen)[:300])
check("4 known developments, last top 3 is run 2's list",
      len(mem3.developments) == 4 and [t["key"] for t in mem3.last_top_k] ==
      ["qwen/qwen-4/release", "meta/llama-5/release", "mistral/large-4/release"], str(mem3.last_top_k))

api.reset()
state = api.load_state()
check("reset: memory is empty again", state["seen_urls"] == [] and state["developments"] == []
      and state["last_run_id"] is None, str(state)[:200])

bad = BackendClient(base, uname, "wrong-password", retry)
try:
    bad.load_state()
    check("wrong tracker password -> TerminalError", False, "no error?!")
except TerminalError as e:
    check("wrong tracker password -> TerminalError with a clear message", "TRACKER_PASSWORD" in str(e), str(e))
down = BackendClient("http://127.0.0.1:9", uname, pw, retry)
try:
    down.load_state()
    check("backend down -> TerminalError", False, "no error?!")
except TerminalError as e:
    check("backend not running -> TerminalError that says so", "Is it running" in str(e), str(e))

tok = httpx.post(base + "/api/auth/login", json={"username": uname, "password": pw}).json()["access_token"]
r = httpx.delete(f"{base}/api/users/{user_id}", headers={"Authorization": f"Bearer {tok}"})
check("cleanup: throwaway user deleted", r.status_code == 204, r.text[:200])
for client in (api, bad, down):
    client.close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
