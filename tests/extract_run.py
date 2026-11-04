"""Copy one run's lines from the three service logs into traces/<label>/<service>.jsonl.

    backend\.venv\Scripts\python.exe tests\extract_run.py <run_id> <label> [--verifier-url URL]

The verifier's lines come from traces/verifier.jsonl, or, for the Render instance, from its
authenticated GET /trace?run_id= (VERIFIER_TOKEN from the environment / .env).
"""
import argparse
import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("run_id")
ap.add_argument("label")
ap.add_argument("--verifier-url")
a = ap.parse_args()
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
out = ROOT / "traces" / a.label
out.mkdir(parents=True, exist_ok=True)
for svc in ("orchestrator", "researcher", "verifier"):
    if svc == "verifier" and a.verifier_url:
        lines = httpx.get(a.verifier_url.rstrip("/") + "/trace", params={"run_id": a.run_id}, timeout=60,
                          headers={"Authorization": f"Bearer {os.environ['VERIFIER_TOKEN']}"}).json()["lines"]
    else:
        p = ROOT / "traces" / f"{svc}.jsonl"
        lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
        lines = [e for e in lines if e.get("run_id") == a.run_id]
    (out / f"{svc}.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines),
                                      encoding="utf-8")
    print(f"{svc}: {len(lines)} lines -> traces/{a.label}/{svc}.jsonl")
