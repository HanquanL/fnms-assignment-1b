"""Start an orchestrated run from the command line, the same way the Tracker page's button does:
log in to the A1 API, POST /api/tracker/orchestrate, poll until the run is saved.

    cd backend
    .venv\\Scripts\\python.exe run_orchestrated.py                 # uses TRACKER_USERNAME / TRACKER_PASSWORD
    .venv\\Scripts\\python.exe run_orchestrated.py --api http://localhost:8000

Prints the run id, status, which worker failed (if any), and the verdict counts.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("TRACKER_API_URL", "http://localhost:8000"))
    args = ap.parse_args()
    s = requests.Session()
    r = s.post(f"{args.api}/api/auth/login", json={"username": os.environ["TRACKER_USERNAME"],
                                                    "password": os.environ["TRACKER_PASSWORD"]}, timeout=30)
    r.raise_for_status()
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    t0 = time.monotonic()
    r = s.post(f"{args.api}/api/tracker/orchestrate", timeout=30)
    if r.status_code != 202:
        print(f"could not start: HTTP {r.status_code} {r.text[:300]}", file=sys.stderr)
        return 1
    run_id = r.json()["run_id"]
    print(f"run {run_id} started")
    stage = None
    while True:
        time.sleep(3)
        p = s.get(f"{args.api}/api/tracker/orchestrate/{run_id}", timeout=30).json()
        if p["stage"] != stage:
            stage = p["stage"]
            print(f"  [{time.monotonic() - t0:6.1f}s] {stage}")
        if p["state"] != "running":
            break
    if p["state"] == "error":
        print(f"run failed before it was saved: {p['error']}", file=sys.stderr)
        return 1
    d = s.get(f"{args.api}/api/tracker/runs/{run_id}", timeout=30).json()
    st = d["stats"]
    print(f"status: {d['status']} | failed workers: {d['failed_workers'] or '-'} | partial: {d['partial_workers'] or '-'}")
    print(f"researcher: {st.get('researcher_status')} ({st.get('researcher_latency_ms')} ms) | "
          f"verifier: {st.get('verifier_status')} ({st.get('verifier_latency_ms')} ms)")
    print(f"charged: {st['charged_steps']}/{st['budget_steps']} steps, {st['charged_fetches']}/{st['budget_fetches']} "
          f"fetches, {st['charged_tokens']:,}/{st['budget_tokens']:,} tokens, {st.get('searches')} searches, "
          f"{st['duration_s']}s")
    print(f"claims: {d['claim_counts']}")
    for rk in d["rankings"]:
        print(f"  #{rk['rank']} [{rk['change']}] {rk['title'][:80]}")
    if d.get("stale_top_k"):
        print(f"  (stale top K from run {d['stale_top_k']['run_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
