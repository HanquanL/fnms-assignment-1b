"""
End-to-end checks for the tracker API , in the same style as api_check.py.
Registers two throwaway users, exercises every /api/tracker endpoint, then deletes them.

Usage (backend running, from the repo root):
    backend\\.venv\\Scripts\\python.exe tests\\tracker_api_check.py
    backend\\.venv\\Scripts\\python.exe tests\\tracker_api_check.py --base-url http://localhost:8000
"""
import argparse
import copy
import sys
import uuid

import requests

XSS_TITLE = "Qwen 4 released <script>alert(1)</script>"
QWEN = "qwen/qwen-4/release"
LLAMA = "meta/llama-5/release"

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


def section(title: str) -> None:
    print(f"\n== {title}")


def show(r: requests.Response) -> str:
    return f"(got {r.status_code}: {r.text[:300]})"


class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def call(self, method, path, token=None, body=None, auth_header=None):
        headers = {}
        if auth_header is not None:
            headers["Authorization"] = auth_header
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return requests.request(method, self.base + path, json=body, headers=headers, timeout=20)


def base_run(started: str = "2026-10-07T20:00:00Z", finished: str = "2026-10-07T20:05:00Z") -> dict:
    """A valid run-1 payload. Tests deep-copy and mutate it."""
    return {
        "started_at": started,
        "finished_at": finished,
        "status": "complete",
        "stop_reason": None,
        "topic": "Open-weight AI model releases",
        "k": 5,
        "model": "gemini-3.5-flash-lite",
        "stats": {"steps": 12, "llm_calls": 12, "searches": 3, "fetches": 4},
        "report_md": "# Test report",
        "articles": [
            {"url": "https://example.com/qwen4?utm_source=x", "canonical_url": "https://example.com/qwen4",
             "title": XSS_TITLE, "status": "fetched", "http_status": 200, "fetched_at": "2026-10-07T20:01:00Z"},
            {"url": "http://127.0.0.1:8000/", "canonical_url": "http://127.0.0.1:8000/", "title": None,
             "status": "rejected", "reason": "host resolves to a loopback address",
             "fetched_at": "2026-10-07T20:02:00Z"},
        ],
        "developments": [
            {"key": QWEN, "title": "Qwen 4 released", "summary": "Test summary.",
             "sources": [{"url": "https://example.com/qwen4", "title": XSS_TITLE,
                          "evidence": "Qwen 4 is available today."}]},
        ],
        "rankings": [{"key": QWEN, "rank": 1, "change": "new", "summary": "Test summary."}],
    }


def register_and_login(api: Api, username: str) -> tuple[str, str] | tuple[None, None]:
    pw = "Password123!"
    r = api.call("POST", "/api/auth/register", body={"username": username, "password": pw})
    if r.status_code != 201:
        return None, None
    user_id = r.json()["id"]
    r = api.call("POST", "/api/auth/login", body={"username": username, "password": pw})
    return (user_id, r.json()["access_token"]) if r.status_code == 200 else (None, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    api = Api(parser.parse_args().base_url)
    sfx = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------------
    section("Rule 2: every tracker endpoint needs a valid token")
    endpoints = [
        ("GET", "/api/tracker/state", None),
        ("DELETE", "/api/tracker/state", None),
        ("GET", "/api/tracker/runs", None),
        ("POST", "/api/tracker/runs", base_run()),
        ("GET", "/api/tracker/runs/latest", None),
        ("GET", f"/api/tracker/runs/{uuid.uuid4()}", None),
    ]
    for method, path, body in endpoints:
        label = path if "runs/" not in path or path.endswith("latest") else "/api/tracker/runs/:id"
        r = api.call(method, path, body=body)
        check(f"{method} {label} without token -> 401", r.status_code == 401, show(r))
        r = api.call(method, path, body=body, auth_header="Bearer not-a-jwt")
        check(f"{method} {label} with garbage token -> 401", r.status_code == 401, show(r))

    a_id, tok_a = register_and_login(api, f"trk_a_{sfx}")
    b_id, tok_b = register_and_login(api, f"trk_b_{sfx}")
    if not (tok_a and tok_b):
        print("\nCannot continue without two users.")
        return 1

    # ------------------------------------------------------------------
    section("Empty state for a new user")
    r = api.call("GET", "/api/tracker/state", token=tok_a)
    s = r.json() if r.status_code == 200 else {}
    check("GET /state -> 200", r.status_code == 200, show(r))
    check("state is empty", s.get("seen_urls") == [] and s.get("developments") == []
          and s.get("last_run_id") is None and s.get("last_top_k") == [], show(r))
    r = api.call("GET", "/api/tracker/runs", token=tok_a)
    check("GET /runs -> 200 []", r.status_code == 200 and r.json() == [], show(r))
    r = api.call("GET", "/api/tracker/runs/latest", token=tok_a)
    check("GET /runs/latest with no runs -> 404", r.status_code == 404, show(r))

    # ------------------------------------------------------------------
    section("Save run 1")
    r = api.call("POST", "/api/tracker/runs", token=tok_a, body=base_run())
    check("POST /runs -> 201", r.status_code == 201, show(r))
    run1 = r.json() if r.status_code == 201 else {}
    run1_id = run1.get("id")
    counts = run1.get("counts", {})
    check("counts: new=1 fetched=1 rejected=1",
          counts.get("new") == 1 and counts.get("fetched") == 1 and counts.get("rejected") == 1, show(r))
    check("stats round-trip", run1.get("stats") == base_run()["stats"], show(r))

    r = api.call("GET", "/api/tracker/state", token=tok_a)
    s = r.json()
    check("seen_urls has only the FETCHED canonical url (not the rejected one)",
          s["seen_urls"] == ["https://example.com/qwen4"], show(r))
    check("one development remembered", len(s["developments"]) == 1 and s["developments"][0]["key"] == QWEN, show(r))
    check("last_top_k is run 1's top K",
          s["last_run_id"] == run1_id and [t["key"] for t in s["last_top_k"]] == [QWEN], show(r))

    r = api.call("GET", "/api/tracker/runs/latest", token=tok_a)
    d = r.json() if r.status_code == 200 else {}
    check("GET /runs/latest -> run 1", r.status_code == 200 and d.get("id") == run1_id, show(r))
    check("latest has ranking with its source",
          d.get("rankings", [{}])[0].get("sources", [{}])[0].get("url") == "https://example.com/qwen4", show(r))
    check("latest lists both articles with statuses",
          sorted(a["status"] for a in d.get("articles", [])) == ["fetched", "rejected"], show(r))
    check("web text is stored verbatim as data (escaping is the frontend's job)",
          any(a["title"] == XSS_TITLE for a in d.get("articles", [])), show(r))

    r = api.call("GET", f"/api/tracker/runs/{run1_id}", token=tok_a)
    check("GET /runs/:id -> 200 run 1", r.status_code == 200 and r.json().get("id") == run1_id, show(r))

    # ------------------------------------------------------------------
    section("Validation: bad payloads -> 422 and nothing is saved")

    def mutated(fn):
        body = copy.deepcopy(base_run())
        fn(body)
        return body

    bad = {
        "javascript: source url": lambda b: b["developments"][0]["sources"][0].update(url="javascript:alert(1)"),
        "rank greater than k": lambda b: b["rankings"][0].update(rank=7),
        "dropped item with a rank": lambda b: b["rankings"][0].update(change="dropped"),
        "ranked item without a rank": lambda b: b["rankings"][0].update(rank=None),
        "duplicate ranks": lambda b: b["rankings"].append({"key": LLAMA, "rank": 1, "change": "new", "summary": "x"}),
        "development without sources": lambda b: b["developments"][0].update(sources=[]),
        "title over 500 chars": lambda b: b["articles"][0].update(title="x" * 501),
        "unknown article status": lambda b: b["articles"][0].update(status="done"),
        "k out of range (11)": lambda b: b.update(k=11),
        "finished before started": lambda b: b.update(finished_at="2026-10-07T19:00:00Z"),
    }
    for name, fn in bad.items():
        r = api.call("POST", "/api/tracker/runs", token=tok_a, body=mutated(fn))
        check(f"{name} -> 422", r.status_code == 422, show(r))

    r = api.call("POST", "/api/tracker/runs", token=tok_a,
                 body=mutated(lambda b: b["rankings"].append(
                     {"key": "unknown/model/release", "rank": 2, "change": "new", "summary": "x"})))
    check("ranking with an unknown development key -> 422", r.status_code == 422, show(r))

    r = api.call("GET", "/api/tracker/runs", token=tok_a)
    check("still exactly 1 run saved (rejected writes are atomic)", len(r.json()) == 1, show(r))

    # ------------------------------------------------------------------
    section("Dedup: run 2 reports the same development from a new URL")
    run2 = base_run("2026-10-08T20:00:00Z", "2026-10-08T20:05:00Z")
    run2["articles"] = [
        {"url": "https://example.com/qwen4", "canonical_url": "https://example.com/qwen4", "title": None,
         "status": "skipped", "reason": "already fetched in an earlier run", "fetched_at": "2026-10-08T20:01:00Z"},
        {"url": "https://other.example.org/qwen-4-news", "canonical_url": "https://other.example.org/qwen-4-news",
         "title": "Alibaba ships Qwen 4", "status": "fetched", "http_status": 200,
         "fetched_at": "2026-10-08T20:02:00Z"},
    ]
    run2["developments"][0]["sources"] = [{"url": "https://other.example.org/qwen-4-news",
                                           "title": "Alibaba ships Qwen 4", "evidence": "Qwen 4 ships today."}]
    run2["rankings"][0]["change"] = "still"
    r = api.call("POST", "/api/tracker/runs", token=tok_a, body=run2)
    c = r.json().get("counts", {}) if r.status_code == 201 else {}
    check("POST run 2 -> 201", r.status_code == 201, show(r))
    check("run 2 counts: still=1 fetched=1 skipped=1",
          c.get("still") == 1 and c.get("fetched") == 1 and c.get("skipped") == 1, show(r))

    s = api.call("GET", "/api/tracker/state", token=tok_a).json()
    check("still ONE development (same key -> same row)", len(s["developments"]) == 1)
    check("that development now has 2 sources", len(s["developments"][0]["sources"]) == 2, str(s["developments"]))
    check("seen_urls grew to 2", len(s["seen_urls"]) == 2, str(s["seen_urls"]))

    # ------------------------------------------------------------------
    section("Dropped items and failed runs")
    run3 = base_run("2026-10-09T20:00:00Z", "2026-10-09T20:05:00Z")
    run3["articles"] = []
    run3["developments"] = [{"key": LLAMA, "title": "Llama 5 released", "summary": "Llama summary.",
                             "sources": [{"url": "https://example.com/llama5", "title": "Llama 5", "evidence": "x"}]}]
    run3["rankings"] = [
        {"key": LLAMA, "rank": 1, "change": "new", "summary": "Llama summary."},
        {"key": QWEN, "rank": None, "change": "dropped", "summary": "Test summary."},
    ]
    r = api.call("POST", "/api/tracker/runs", token=tok_a, body=run3)
    run3_id = r.json().get("id") if r.status_code == 201 else None
    c = r.json().get("counts", {}) if r.status_code == 201 else {}
    check("POST run 3 -> 201 with new=1 dropped=1",
          r.status_code == 201 and c.get("new") == 1 and c.get("dropped") == 1, show(r))

    r = api.call("GET", "/api/tracker/runs/latest", token=tok_a)
    rk = r.json().get("rankings", [])
    check("latest lists ranked items first, then dropped (rank null)",
          [(x["key"], x["rank"], x["change"]) for x in rk] == [(LLAMA, 1, "new"), (QWEN, None, "dropped")],
          show(r))

    run4 = base_run("2026-10-10T20:00:00Z", "2026-10-10T20:00:30Z")
    run4.update(status="failed", stop_reason="terminal: invalid API key", report_md="",
                articles=[], developments=[], rankings=[])
    r = api.call("POST", "/api/tracker/runs", token=tok_a, body=run4)
    check("POST failed run 4 -> 201", r.status_code == 201, show(r))
    r = api.call("GET", "/api/tracker/runs/latest", token=tok_a)
    check("failed run is NOT the latest report (still run 3)", r.json().get("id") == run3_id, show(r))
    s = api.call("GET", "/api/tracker/state", token=tok_a).json()
    check("failed run is NOT used as memory (last_run_id = run 3)", s["last_run_id"] == run3_id)
    check("last_top_k excludes dropped items", [t["key"] for t in s["last_top_k"]] == [LLAMA], str(s["last_top_k"]))
    r = api.call("GET", "/api/tracker/runs", token=tok_a)
    runs = r.json()
    check("history lists all 4 runs, newest first",
          len(runs) == 4 and runs[0]["status"] == "failed" and runs[-1]["id"] == run1_id, show(r))

    # ------------------------------------------------------------------
    section("Rule 3: user B can't see or touch user A's tracker data")
    r_other = api.call("GET", f"/api/tracker/runs/{run1_id}", token=tok_b)
    r_missing = api.call("GET", f"/api/tracker/runs/{uuid.uuid4()}", token=tok_b)
    r_bad = api.call("GET", "/api/tracker/runs/not-a-uuid", token=tok_b)
    check("B reading A's run -> 404", r_other.status_code == 404, show(r_other))
    check("indistinguishable from a missing / malformed id",
          r_other.status_code == r_missing.status_code == r_bad.status_code and r_other.text == r_missing.text,
          f"{r_other.text} | {r_missing.text} | {r_bad.text}")
    r = api.call("GET", "/api/tracker/runs", token=tok_b)
    check("B's history is empty", r.status_code == 200 and r.json() == [], show(r))
    r = api.call("GET", "/api/tracker/state", token=tok_b)
    check("B's state is empty", r.json().get("developments") == [] and r.json().get("seen_urls") == [], show(r))

    r = api.call("POST", "/api/tracker/runs", token=tok_b, body=base_run())
    check("B can use the same development key as A -> 201", r.status_code == 201, show(r))
    r = api.call("DELETE", "/api/tracker/state", token=tok_b)
    check("B resets B's state -> 204", r.status_code == 204, show(r))
    r = api.call("GET", "/api/tracker/runs", token=tok_a)
    check("A still has 4 runs after B's reset", len(r.json()) == 4, show(r))
    s = api.call("GET", "/api/tracker/state", token=tok_a).json()
    check("A's developments untouched by B", sorted(d["key"] for d in s["developments"]) == sorted([QWEN, LLAMA]))

    # ------------------------------------------------------------------
    section("Reset")
    r = api.call("DELETE", "/api/tracker/state", token=tok_a)
    check("DELETE /state -> 204", r.status_code == 204, show(r))
    s = api.call("GET", "/api/tracker/state", token=tok_a).json()
    check("state empty after reset", s["seen_urls"] == [] and s["developments"] == [] and s["last_run_id"] is None)
    r = api.call("GET", "/api/tracker/runs", token=tok_a)
    check("history empty after reset", r.json() == [], show(r))
    r = api.call("GET", "/api/tracker/runs/latest", token=tok_a)
    check("latest -> 404 after reset", r.status_code == 404, show(r))

    # ------------------------------------------------------------------
    section("Cascade + cleanup")
    api.call("POST", "/api/tracker/runs", token=tok_a, body=base_run())
    r = api.call("DELETE", f"/api/users/{a_id}", token=tok_a)
    check("deleting a user who HAS tracker data -> 204 (FK cascade)", r.status_code == 204, show(r))
    r = api.call("DELETE", f"/api/users/{b_id}", token=tok_b)
    check("delete user B -> 204", r.status_code == 204, show(r))

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())