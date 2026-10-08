"""Run the tracker.

    python -m tracker run --label run1   # a real run: memory from the A1 backend, results saved back
    python -m tracker run                # same, labelled with a timestamp
    python -m tracker status             # what the tracker remembers
    python -m tracker reset              # forget everything (asks first; --yes to skip)
    python -m tracker save <file.json>   # retry saving a run the backend didn't accept
    python -m tracker run --local        # development: no memory, no backend

A real run writes reports/<label>.md and traces/<label>.jsonl.
Exit code: 0 complete, 2 partial, 1 failed / stopped.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from tracker.agent import run_agent
from tracker.backend import BackendClient
from tracker.config import ROOT, load_config
from tracker.env import secret
from tracker.errors import RetryPolicy, TerminalError
from tracker.llm.gemini import GeminiClient
from tracker.memory import build_payload, classify, dedupe_against_memory, memory_from_state
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


def make_backend(cfg) -> BackendClient:
    url = os.environ.get("TRACKER_API_URL") or (cfg.get("backend") or {}).get("api_url", "http://localhost:8000")
    return BackendClient(url, secret("TRACKER_USERNAME"), secret("TRACKER_PASSWORD"),
                         RetryPolicy.from_config(cfg["retry"]))


def _summary_line(result) -> str:
    st = result.stats
    return (f"developments: {len(result.report['developments'])} | model calls {st['steps']} | searches "
            f"{st['searches']} | fetches {st['fetches']} | tokens {st['tokens_total']:,} | {st['duration_s']}s")


def cmd_run(label: str | None, force: bool) -> int:
    cfg = load_config()
    label = label or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path, trace_path = ROOT / "reports" / f"{label}.md", ROOT / "traces" / f"{label}.jsonl"
    if not force and (report_path.exists() or trace_path.exists()):
        print(f"STOPPED: {report_path.relative_to(ROOT)} or {trace_path.relative_to(ROOT)} already exists "
              f"(use another --label, or --force to overwrite)", file=sys.stderr)
        return 1
    if force:
        trace_path.unlink(missing_ok=True)

    # Everything that can fail cheaply fails before the first model call
    try:
        llm, fallback = make_llms(cfg)
        secret("TAVILY_API_KEY")
        backend = make_backend(cfg)
        t0 = time.monotonic()
        state = backend.load_state()  # login + GET /api/tracker/state
        load_ms = int((time.monotonic() - t0) * 1000)
    except TerminalError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1
    memory = memory_from_state(state)
    first_run = state.get("last_run_id") is None

    trace = Trace(trace_path)
    try:
        trace.log("memory", status="loaded", latency_ms=load_ms, requests=2, seen_urls=len(memory.seen_urls),
                  developments=len(memory.developments), last_top_k=[t["key"] for t in memory.last_top_k])
        result = run_agent(cfg, llm, search_web, fetch_article, trace, memory=memory, fallback_llm=fallback)

        renames = dedupe_against_memory(result.report, memory)
        changes, dropped = classify(result.report, memory, run_complete=result.status == "complete")
        trace.log("memory", status="classified", renamed_keys=renames or None,
                  changes=changes, dropped=[t["key"] for t in dropped])

        report_path.parent.mkdir(parents=True, exist_ok=True)
        md = render_markdown(cfg, result, changes=None if first_run else changes, dropped=dropped, run_label=label)
        report_path.write_text(md, encoding="utf-8")
        payload = build_payload(cfg, result, md, changes, dropped, result.model or cfg["model"]["name"])

        try:
            t0 = time.monotonic()
            saved = backend.save_run(payload)
            trace.log("memory", status="saved", latency_ms=int((time.monotonic() - t0) * 1000), requests=1,
                      run_id=saved["id"], counts=saved.get("counts"))
            saved_line = f"saved:  run {saved['id']}"
        except TerminalError as e:
            # Don't lose a finished run because the backend was down at the end
            unsaved = ROOT / "reports" / f"unsaved-{label}.json"
            unsaved.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            trace.log("memory", status="save_failed", detail=str(e), file=str(unsaved.name))
            saved_line = (f"NOT SAVED: {e}\n        kept in {unsaved.relative_to(ROOT)}; once the backend is up: "
                          f"python -m tracker save {unsaved.relative_to(ROOT)}")
    finally:
        trace.close()
        backend.close()

    n_new = sum(1 for v in changes.values() if v == "new")
    n_still = len(changes) - n_new
    print(f"status: {result.status}" + (f" ({result.stop_reason})" if result.stop_reason else ""))
    print(_summary_line(result))
    print(f"memory: {len(memory.seen_urls)} URLs and {len(memory.developments)} developments known before this run"
          f" | new {n_new} | still {n_still} | dropped {len(dropped)}"
          + (f" | keys matched to known developments: {renames}" if renames else ""))
    print(f"report: {report_path.relative_to(ROOT)}\ntrace:  {trace_path.relative_to(ROOT)}\n{saved_line}")
    if saved_line.startswith("NOT SAVED"):
        return 1
    return {"complete": 0, "partial": 2}.get(result.status, 1)


def cmd_run_local() -> int:
    cfg = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    trace_path = ROOT / "traces" / "dev" / f"{stamp}.jsonl"
    report_path = ROOT / "reports" / "dev" / f"{stamp}.md"
    try:
        llm, fallback = make_llms(cfg)
        secret("TAVILY_API_KEY")
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
    print(f"status: {result.status}" + (f" ({result.stop_reason})" if result.stop_reason else ""))
    print(_summary_line(result))
    print(f"report: {report_path.relative_to(ROOT)}\ntrace:  {trace_path.relative_to(ROOT)}")
    return {"complete": 0, "partial": 2}.get(result.status, 1)


def cmd_status() -> int:
    backend = make_backend(load_config())
    try:
        state = backend.load_state()
    finally:
        backend.close()
    print(f"seen URLs: {len(state['seen_urls'])} | known developments: {len(state['developments'])}")
    print(f"last run: {state['last_run_id'] or '(none yet)'} at {state['last_run_finished_at'] or '-'}")
    for t in state["last_top_k"]:
        print(f"  #{t['rank']} {t['key']}: {t['title']}")
    return 0


def cmd_reset(yes: bool) -> int:
    if not yes:
        answer = input("Delete ALL saved tracker runs, developments, and seen URLs for this user? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return 1
    backend = make_backend(load_config())
    try:
        backend.reset()
    finally:
        backend.close()
    print("Tracker state reset: the next run starts with no memory.")
    return 0


def cmd_save(path: str) -> int:
    backend = make_backend(load_config())
    try:
        with open(path, encoding="utf-8") as f:
            saved = backend.save_run(json.load(f))
    finally:
        backend.close()
    print(f"saved: run {saved['id']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run the tracker once")
    p.add_argument("--label", help="name for reports/<label>.md and traces/<label>.jsonl, e.g. run1")
    p.add_argument("--force", action="store_true", help="overwrite an existing report/trace with this label")
    p.add_argument("--local", action="store_true", help="development: no memory, no backend")
    sub.add_parser("status", help="show what the tracker remembers")
    p = sub.add_parser("reset", help="forget all saved tracker state")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("save", help="save a run file left behind when the backend was down")
    p.add_argument("file")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "run":
            return cmd_run_local() if args.local else cmd_run(args.label, args.force)
        if args.cmd == "status":
            return cmd_status()
        if args.cmd == "reset":
            return cmd_reset(args.yes)
        if args.cmd == "save":
            return cmd_save(args.file)
    except TerminalError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
