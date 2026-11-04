"""
Offline checks for the researcher worker: the A1B agent loop behind POST /research, driven by a
scripted fake model, fake search and fake fetch (no API calls, no credits, no network).

Usage (from the repo root, backend venv):
    backend\\.venv\\Scripts\\python.exe tests\\researcher_check.py
"""
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["RESEARCHER_NO_AUTOSTART"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from researcher.app import build_app  # noqa: E402
from researcher.core import Deps  # noqa: E402
from shared.contract import ResearchResult, WorkerResponse  # noqa: E402
from shared.tracelog import REQUIRED, TraceLog  # noqa: E402
from shared.workerkit import Cooldown  # noqa: E402
from tracker.config import load_config  # noqa: E402
from tracker.errors import RetriesExhausted, TerminalError  # noqa: E402
from tracker.fetch import FetchResult, fetch_article  # noqa: E402
from tracker.llm.base import ModelTurn, Pacer, ToolCall, Usage  # noqa: E402
from tracker.search import SearchHit, SearchResponse  # noqa: E402
from tracker.urlguard import FetchPolicy  # noqa: E402
from tracker.urls import canonicalize  # noqa: E402

TOKEN = "test-researcher-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "https://example.com/qwen-4"
REDIRECTING = "https://short.example/q4"
TEXT = "Alibaba released Qwen 4 today under the Apache 2.0 license. The 72B model is on Hugging Face."
EVIDENCE = "Alibaba released Qwen 4 today under the Apache 2.0 license."
GOOD = {"developments": [{"key": "qwen/qwen-4/release", "title": "Qwen 4 released", "rank": 1,
                          "claims": [{"text": "Alibaba released Qwen 4 under Apache 2.0.", "url": URL,
                                      "evidence": EVIDENCE}]}]}
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))


def turn(*calls) -> ModelTurn:
    return ModelTurn(text="", tool_calls=[ToolCall(id=None, name=n, args=a) for n, a in calls],
                     usage=Usage(100, 20, 0, 120), finish_reason="STOP", raw=[])


HAPPY = [turn(("search_web", {"query": "qwen 4"})), turn(("fetch_article", {"url": URL})), turn(("finish", GOOD))]


class FakeLLM:
    provider, model = "fake", "fake-model"

    def __init__(self, turns, *, block: threading.Event | None = None, error=None):
        self.turns, self.calls, self.block, self.error = turns, 0, block, error
        self.call_timeout = None

    def generate(self, system, history, tools):
        self.calls += 1
        if self.block is not None:
            self.block.wait(5)
        if self.error is not None:
            raise self.error
        return self.turns[min(self.calls, len(self.turns)) - 1]


class FakeWeb:
    def __init__(self):
        self.searches, self.requests = 0, []

    def search_raw(self, query, timeout):
        self.searches += 1
        return SearchResponse(query, [SearchHit("Qwen 4", URL, "snippet", None, 0.9)], credits=1)

    def fetch_raw(self, url, on_hop=None, deadline=None):
        hops = [url, URL] if url == REDIRECTING else [url]   # the short link redirects once
        sent = 0
        for hop in hops:
            if on_hop is not None and not on_hop(hop):
                return FetchResult("failed", url, canonicalize(url), reason="fetch budget used up", hops=sent)
            self.requests.append(hop)
            sent += 1
        return FetchResult("fetched", url, canonicalize(url), URL, "Qwen 4", TEXT, 200, hops=sent)


def make(llm: FakeLLM, web: FakeWeb | None = None, cooldown: Cooldown | None = None):
    web = web or FakeWeb()
    deps = Deps(base_cfg=load_config(), llm=llm, fallback_llm=None, search_raw=web.search_raw,
                fetch_raw=web.fetch_raw, max_searches=8, pacer=Pacer(1e9, sleep=lambda s: None),
                sleep=lambda s: None, cooldown=cooldown or Cooldown())
    path = Path(os.environ.get("TEMP", ".")) / f"fnms-researcher-check-{uuid.uuid4().hex}.jsonl"
    return TestClient(build_app(deps, token=TOKEN, trace=TraceLog("researcher", path))), web, path


def envelope(topic="Open-weight AI model releases", k=1, *, time_budget_ms=60000, budget=None, request_id=None):
    return {"request_id": request_id or str(uuid.uuid4()), "run_id": str(uuid.uuid4()),
            "time_budget_ms": time_budget_ms,
            "budget": budget or {"max_steps": 10, "max_fetches": 8, "max_tokens": 40000},
            "payload": {"topic": topic, "k": k}}


def valid(body) -> bool:
    try:
        r = WorkerResponse.model_validate(body)
        if r.result is not None:
            ResearchResult.model_validate(r.result)
        return True
    except Exception as e:  # noqa: BLE001
        print("      invalid:", e)
        return False


print("health, auth, schema")
llm = FakeLLM(HAPPY)
client, web, _ = make(llm)
check("GET /healthz, no token -> 200 {status: ok}", client.get("/healthz").json() == {"status": "ok"})
body = envelope()
check("no token -> 401", client.post("/research", json=body).status_code == 401)
check("wrong token -> 401", client.post("/research", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401)
r = client.post("/research", content=b"][", headers={"Content-Type": "application/json"})
check("no token AND malformed body -> 401", r.status_code == 401 and "topic" not in r.text)
for name, bad in {"k = 0": {**body, "payload": {"topic": "x", "k": 0}},
                  "k as a string": {**body, "payload": {"topic": "x", "k": "5"}},
                  "k = 11": {**body, "payload": {"topic": "x", "k": 11}},
                  "missing topic": {**body, "payload": {"k": 5}},
                  "empty topic": {**body, "payload": {"topic": "", "k": 5}},
                  "extra payload field (memory)": {**body, "payload": {"topic": "x", "k": 5, "known_keys": []}},
                  "run_id not a UUID": {**body, "run_id": "run-1"},
                  "time_budget_ms = 0": {**body, "time_budget_ms": 0}}.items():
    r = client.post("/research", json=bad, headers=AUTH)
    check(f"{name} -> 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
check("no model calls for rejected requests", llm.calls == 0)

print("\nhappy path and the wire shape")
r = client.post("/research", json=body, headers=AUTH)
out = r.json()
check("200 complete", r.status_code == 200 and out["status"] == "complete", r.text[:300])
check("matches the contract (ResearchResult)", valid(out))
dev = out["result"]["developments"][0]
check("development id is the stable key", dev["id"] == "qwen/qwen-4/release")
check("claim has claim_id, text, source_url only", set(dev["claims"][0]) == {"claim_id", "text", "source_url"}
      and dev["claims"][0]["claim_id"] == "qwen/qwen-4/release#1" and dev["claims"][0]["source_url"] == URL)
check("no evidence quote, article text or reasoning on the wire",
      "evidence" not in r.text and "Hugging Face" not in r.text and "untrusted_web_content" not in r.text)
check("usage reported: 3 model calls, 1 search, 1 fetch, 360 tokens",
      out["usage"] == {"steps": 3, "fetches": 1, "tokens": 360, "searches": 1}, str(out["usage"]))

print("\nidempotency")
calls, reqs, searches = llm.calls, len(web.requests), web.searches
r2 = client.post("/research", content=json.dumps(dict(reversed(list(body.items())))).encode(),
                 headers={**AUTH, "Content-Type": "application/json"})
check("same id + same JSON (other key order) -> stored response", r2.status_code == 200 and r2.json() == out)
check("replay: no model calls, searches or fetches",
      (llm.calls, len(web.requests), web.searches) == (calls, reqs, searches))
check("same id + k changed -> 409", client.post("/research", json={**body, "payload": {**body["payload"], "k": 2}},
                                                headers=AUTH).status_code == 409)
check("same id + time_budget_ms shrunk -> 409",
      client.post("/research", json={**body, "time_budget_ms": 59000}, headers=AUTH).status_code == 409)

gate = threading.Event()
llm = FakeLLM(HAPPY, block=gate)
client, web, _ = make(llm)
body = envelope()
first: dict = {}
t = threading.Thread(target=lambda: first.setdefault("r", client.post("/research", json=body, headers=AUTH)))
t.start()
for _ in range(100):
    if llm.calls:
        break
    time.sleep(0.02)
r2 = client.post("/research", json=body, headers=AUTH)
check("same id while running -> 503 + Retry-After", r2.status_code == 503 and r2.headers.get("retry-after"), r2.text)
check("... and the loop was not started twice", llm.calls == 1)
gate.set()
t.join(10)
check("first copy finishes normally; a later retry replays it",
      first["r"].status_code == 200 and client.post("/research", json=body, headers=AUTH).json() == first["r"].json()
      and llm.calls == 3)
r3 = client.post("/research", json=envelope(), headers=AUTH)
check("a different request_id is new work", r3.status_code == 200 and llm.calls > 3)

print("\nbudgets and deadlines -> partial")
llm = FakeLLM(HAPPY)
client, web, _ = make(llm)
r = client.post("/research", json=envelope(time_budget_ms=1), headers=AUTH)
out = r.json()
check("time_budget_ms=1 -> 200 partial, time_budget_exhausted, no model call",
      r.status_code == 200 and out["status"] == "partial" and out["error"]["code"] == "time_budget_exhausted"
      and out["result"] == {"developments": []} and llm.calls == 0 and valid(out), r.text[:300])

llm = FakeLLM([turn(("search_web", {"query": "q"})), turn(("fetch_article", {"url": URL})),
               turn(("finish", GOOD))])   # 3rd call is the forced, finish-only call
client, web, _ = make(llm)
r = client.post("/research", json=envelope(budget={"max_steps": 3, "max_fetches": 8, "max_tokens": 40000}),
                headers=AUTH)
out = r.json()
check("max_steps=3 -> partial budget_exhausted, keeps the verified development (held-back finish step)",
      out["status"] == "partial" and out["error"]["code"] == "budget_exhausted"
      and len(out["result"]["developments"]) == 1 and out["usage"]["steps"] <= 3, r.text[:400])

llm = FakeLLM([turn(("fetch_article", {"url": REDIRECTING})), turn(("finish", GOOD))])
client, web, _ = make(llm)
r = client.post("/research", json=envelope(), headers=AUTH)
check("a redirect counts as 2 fetches (one per hop)", r.json()["usage"]["fetches"] == 2, r.text[:300])
llm = FakeLLM([turn(("fetch_article", {"url": REDIRECTING})), turn(("finish", GOOD))])
client, web, _ = make(llm)
r = client.post("/research", json=envelope(budget={"max_steps": 10, "max_fetches": 1, "max_tokens": 40000}),
                headers=AUTH)
check("max_fetches=1 and a redirect: the 2nd hop is refused before it is sent",
      r.json()["usage"]["fetches"] == 1 and web.requests == [REDIRECTING], f"{r.text[:300]} {web.requests}")

print("\nthe tracker's real fetch_article honours on_hop (no network needed)")
policy = FetchPolicy.from_config(load_config()["fetch"])
seen: list[str] = []
res = fetch_article("http://169.254.169.254/latest/meta-data/", policy, on_hop=lambda u: seen.append(u) or True)
check("metadata URL rejected by the guard; on_hop never asked (no request)",
      res.status == "rejected" and seen == [] and res.hops == 0, str(res))
res = fetch_article("http://93.184.215.14/", policy, on_hop=lambda u: False)
check("public literal IP, on_hop refuses -> failed before any request",
      res.status == "failed" and "budget" in (res.reason or "") and res.hops == 0, str(res))

print("\nmodel failures")
llm = FakeLLM(HAPPY, error=TerminalError("Gemini rejected the API key (400 INVALID_ARGUMENT)"))
client, web, _ = make(llm)
b = envelope()
r = client.post("/research", json=b, headers=AUTH)
check("bad model key, nothing found -> 500 failed, retryable false",
      r.status_code == 500 and r.json()["status"] == "failed" and r.json()["error"]["retryable"] is False
      and valid(r.json()), r.text[:300])
check("stored: same id -> same 500, no new model call",
      client.post("/research", json=b, headers=AUTH).status_code == 500 and llm.calls == 1)

cool = Cooldown()
llm = FakeLLM(HAPPY, error=RetriesExhausted("gemini model call: still failing after 6 attempts (429)"))
client, web, _ = make(llm, cooldown=cool)
r = client.post("/research", json=envelope(), headers=AUTH)
check("rate-limited through every retry -> 200 partial model_unavailable",
      r.status_code == 200 and r.json()["error"]["code"] == "model_unavailable", r.text[:300])
r = client.post("/research", json=envelope(), headers=AUTH)
check("next NEW request during the cooldown -> 503 + Retry-After, no work",
      r.status_code == 503 and r.headers.get("retry-after", "").isdigit() and llm.calls == 1, r.text)

print("\ntrace and isolation")
llm = FakeLLM(HAPPY)
client, web, path = make(llm)
b = envelope()
client.post("/research", json=b, headers=AUTH)
lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
check("every trace line has the 11 required fields", all(all(f in e for f in REQUIRED) for e in lines))
mine = [e for e in lines if e["request_id"] == b["request_id"]]
check("inbound line + model/search/fetch/finish calls carry this run_id and request_id",
      any(e["direction"] == "in" for e in mine) and
      {"search_web", "fetch_article", "finish"} <= {e["endpoint_or_tool"] for e in mine}
      and sum(1 for e in mine if e["endpoint_or_tool"].startswith("model:")) == 3
      and all(e["run_id"] == b["run_id"] for e in mine), str([e["endpoint_or_tool"] for e in mine]))
check("token never in the trace", TOKEN not in path.read_text(encoding="utf-8"))
src = "".join((Path(__file__).resolve().parent.parent / "researcher" / f).read_text(encoding="utf-8")
              for f in ("app.py", "core.py"))
check("researcher code imports no database, A1 API client, or memory module",
      all(s not in src for s in ("tracker.backend", "tracker.memory", "from app", "sqlalchemy", "DATABASE_URL")))
check("researcher module graph: tracker.backend / tracker.memory not loaded",
      "tracker.backend" not in sys.modules and "tracker.memory" not in sys.modules)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
