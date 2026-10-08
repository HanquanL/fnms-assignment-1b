"""The agent's tools, callable without the model (requirement 2):

    python -m tracker.tools fetch_article <url> [--full]

Exit code: 0 fetched, 2 rejected by a guardrail, 1 failed.
"""
import argparse
import json
import sys

from tracker.config import load_config
from tracker.fetch import FetchResult, fetch_article as _fetch
from tracker.urlguard import FetchPolicy


def fetch_article(url: str) -> FetchResult:
    return _fetch(url, FetchPolicy.from_config(load_config()["fetch"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tracker.tools")
    sub = parser.add_subparsers(dest="tool", required=True)
    p_fetch = sub.add_parser("fetch_article", help="fetch one URL through the guardrails")
    p_fetch.add_argument("url")
    p_fetch.add_argument("--full", action="store_true", help="print the whole text, not a preview")
    args = parser.parse_args(argv)

    if args.tool == "fetch_article":
        r = fetch_article(args.url).to_dict()
        if not args.full and len(r["text"]) > 600:
            r["text"] = r["text"][:600] + f"... [{len(r['text']):,} chars total; use --full]"
        print(json.dumps(r, indent=2, ensure_ascii=True))
        return {"fetched": 0, "rejected": 2}.get(r["status"], 1)
    return 1


if __name__ == "__main__":
    sys.exit(main())