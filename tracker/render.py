"""Turn a run into Markdown (reports/*.md). Code writes the layout; the model
only supplies the verified developments.

With `changes` (second run onward) the report is organized as the assignment
asks: New since last run, Still in top K, Dropped.
"""
from datetime import datetime


def _md(text: str | None) -> str:
    """Web text into Markdown as text: neutralize characters that would turn it into links/HTML."""
    text = " ".join((text or "").split())
    for ch in ("\\", "[", "]", "<", ">", "`", "*", "_", "#", "|"):
        text = text.replace(ch, "\\" + ch)
    return text


def _url(url: str) -> str:
    return url.replace(" ", "%20").replace(")", "%29").replace("(", "%28").replace("<", "%3C").replace(">", "%3E")


def _dev_block(d: dict) -> list[str]:
    lines = [f"### {d['rank']}. {_md(d['title'])}", f"`{d['key']}`", "", _md(d["summary"]), "", "Evidence:"]
    if d.get("claims"):
        for c in d["claims"]:
            lines.append(f"- {_md(c['text'])}  \n  Source: <{_url(c['url'])}>  \n  > {_md(c['evidence'])}")
    else:
        for s in d["sources"]:
            lines.append(f"- <{_url(s['url'])}>  \n  > {_md(s['evidence'])}")
    lines.append("")
    return lines


def render_markdown(cfg, result, changes: dict | None = None, dropped: list[dict] | None = None,
                    run_label: str = "") -> str:
    r, st = result.report, result.stats
    when = datetime.fromisoformat(result.started_at).strftime("%Y-%m-%d %H:%M UTC")
    lim = cfg["limits"]
    out = [
        f"# {_md(cfg['topic'])}: top {cfg['k']}{(' (' + run_label + ')') if run_label else ''}",
        "",
        f"- Run: {when}",
        f"- Status: **{result.status}**" + (f" ({_md(result.stop_reason)})" if result.stop_reason else ""),
        f"- Model: `{cfg['model']['name']}`",
        f"- Usage: {st['steps']}/{lim['max_steps']} model calls, {st['searches']}/{lim['max_searches']} searches, "
        f"{st['fetches']}/{lim['max_fetches']} fetches, {st['tokens_total']:,} tokens, {st['duration_s']}s",
        "",
    ]
    devs = r["developments"]
    if changes is None:
        out.append(f"## Top {cfg['k']}")
        out.append("")
        for d in devs:
            out += _dev_block(d)
    else:
        new = [d for d in devs if changes.get(d["key"]) == "new"]
        still = [d for d in devs if changes.get(d["key"]) == "still"]
        out += ["## New since last run", ""] + (sum((_dev_block(d) for d in new), []) or ["_Nothing new._", ""])
        out += ["## Still in top K", ""] + (sum((_dev_block(d) for d in still), []) or ["_None._", ""])
        out += ["## Dropped", ""]
        out += [f"- {_md(d['title'])} (`{d['key']}`, was #{d['rank']})" for d in (dropped or [])] or ["_None._"]
        out.append("")
    if not devs:
        out += ["_No verified developments in this run._", ""]
    if r.get("notes"):
        out += ["## Notes", "", _md(r["notes"]), ""]
    if r.get("evidence") and not devs:
        out += ["## Evidence collected (unranked)", ""]
        out += [f"- {_md(e['title'] or 'untitled')}: <{_url(e['url'])}>" for e in r["evidence"]] + [""]
    out += ["## Articles this run", "", "| Status | Title | URL | Reason |", "|---|---|---|---|"]
    for a in result.articles:
        out.append(f"| {a['status']} | {_md(a['title'] or '')[:80]} | {_md(a['url'])[:100]} | {_md(a['reason'] or '')[:80]} |")
    out.append("")
    return "\n".join(out)
