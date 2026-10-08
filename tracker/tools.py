"""The agent's tools, callable without the model (requirement 2):

    python -m tracker.tools fetch_article <url> [--full]
    python -m tracker.tools search_web "<query>"
    python -m tracker.tools finish <report.json>

Exit codes: 0 ok | 1 failed (fetch failed, or a terminal error such as a bad
key) | 2 rejected by a guardrail or by report validation | 3 invalid input.
"""
import argparse
import json
import sys
from pathlib import Path

from tracker.config import load_config
from tracker.errors import RetryPolicy, TerminalError, ToolInputError, call_with_retry
from tracker.env import secret
from tracker.fetch import FetchResult, fetch_article as _fetch
from tracker.report import FinishResult, validate_report
from tracker.search import SearchResponse, tavily_search
from tracker.urlguard import FetchPolicy


def fetch_article(url: str) -> FetchResult:
    return _fetch(url, FetchPolicy.from_config(load_config()["fetch"]))


def search_web(query: str, on_retry=None) -> SearchResponse:
    """Retries transient failures; raises TerminalError (incl. RetriesExhausted) or ToolInputError."""
    cfg = load_config()
    key = secret("TAVILY_API_KEY")
    return call_with_retry(
        lambda: tavily_search(query, api_key=key, cfg=cfg["search"]),
        RetryPolicy.from_config(cfg["retry"]), what="search_web", on_retry=on_retry,
    )


def finish(report: dict, fetched_texts: dict[str, str] | None = None) -> FinishResult:
    return validate_report(report, int(load_config()["k"]), fetched_texts)


def _print(obj: dict) -> None:
    # ensure_ascii: web text can't smuggle terminal escape sequences into the console
    print(json.dumps(obj, indent=2, ensure_ascii=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tracker.tools")
    sub = parser.add_subparsers(dest="tool", required=True)
    p = sub.add_parser("fetch_article", help="fetch one URL through the guardrails")
    p.add_argument("url")
    p.add_argument("--full", action="store_true", help="print the whole text, not a preview")
    p = sub.add_parser("search_web", help="search the web (uses 1 Tavily credit)")
    p.add_argument("query")
    p = sub.add_parser("finish", help="validate a report JSON file (schema only, no provenance check)")
    p.add_argument("report_file")
    args = parser.parse_args(argv)

    def on_retry(attempt, err, delay):
        print(f"[retry] attempt {attempt} failed: {err}; waiting {delay:.1f}s", file=sys.stderr)

    try:
        if args.tool == "fetch_article":
            r = fetch_article(args.url).to_dict()
            if not args.full and len(r["text"]) > 600:
                r["text"] = r["text"][:600] + f"... [{len(r['text']):,} chars total; use --full]"
            _print(r)
            return {"fetched": 0, "rejected": 2}.get(r["status"], 1)

        if args.tool == "search_web":
            _print(search_web(args.query, on_retry=on_retry).to_dict())
            return 0

        if args.tool == "finish":
            report = json.loads(Path(args.report_file).read_text(encoding="utf-8"))
            res = finish(report)
            _print({"ok": res.ok, "problems": res.problems, "report": res.report})
            return 0 if res.ok else 2

    except ToolInputError as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 3
    except TerminalError as e:
        print(f"STOPPED ({type(e).__name__}): {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())