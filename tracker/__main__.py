"""Run the tracker.

    python -m tracker run --local     # one run, no memory, no database (development)

Development runs write reports/dev/<timestamp>.md and traces/dev/<timestamp>.jsonl
(git-ignored); the graded runs come from the full `run` command.
Exit code: 0 complete, 1 failed, 2 partial.
"""
import argparse
import sys
from datetime import datetime, timezone

from tracker.agent import run_agent
from tracker.config import ROOT, load_config
from tracker.env import secret
from tracker.errors import TerminalError
from tracker.llm.gemini import GeminiClient
from tracker.render import render_markdown
from tracker.tools import fetch_article, search_web
from tracker.trace import Trace


def make_llms(cfg):
    """(main model, fallback model or None), both from config.yaml."""
    m = cfg["model"]
    if m["provider"] != "gemini":
        raise TerminalError(f"model.provider '{m['provider']}' is not supported (only 'gemini')")
    key = secret("GEMINI_API_KEY")
    opts = {"temperature": float(m["temperature"]), "max_output_tokens": int(m["max_output_tokens"])}
    fallback = GeminiClient(m["fallback_name"], key, **opts) if m.get("fallback_name") else None
    return GeminiClient(m["name"], key, **opts), fallback


def cmd_run_local() -> int:
    cfg = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    trace_path = ROOT / "traces" / "dev" / f"{stamp}.jsonl"
    report_path = ROOT / "reports" / "dev" / f"{stamp}.md"
    try:
        llm, fallback = make_llms(cfg)
        secret("TAVILY_API_KEY")  # fail fast, before the first model call
    except TerminalError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1

    trace = Trace(trace_path)
    try:
        result = run_agent(cfg, llm, search_web, fetch_article, trace, fallback_llm=fallback)
    finally:
        trace.close()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(cfg, result), encoding="utf-8")

    st = result.stats
    print(f"status: {result.status}" + (f" ({result.stop_reason})" if result.stop_reason else ""))
    print(f"developments: {len(result.report['developments'])} | model calls {st['steps']} | searches "
          f"{st['searches']} | fetches {st['fetches']} | tokens {st['tokens_total']:,} | {st['duration_s']}s")
    print(f"report: {report_path.relative_to(ROOT)}\ntrace:  {trace_path.relative_to(ROOT)}")
    return {"complete": 0, "partial": 2}.get(result.status, 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the tracker once")
    p.add_argument("--local", action="store_true", help="no memory, no database (development)")
    args = parser.parse_args(argv)
    if args.cmd == "run" and args.local:
        return cmd_run_local()
    print("Only 'run --local' exists so far (memory + database come next).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())