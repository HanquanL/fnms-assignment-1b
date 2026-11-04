"""The final report, written by code from VALIDATED worker data only (PDF §5, §11).

Web-derived text (titles, summaries, claims, evidence, URLs) is escaped so it stays text in
Markdown; the React page renders it as text anyway (never as HTML).
"""
import re
from datetime import datetime

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
LABELS = {"supported": "SUPPORTED", "unsupported": "UNSUPPORTED", "unreachable": "UNREACHABLE",
          "unverified": "UNVERIFIED"}


def clean_text(text: str | None, limit: int | None = None) -> str:
    """Strip control / bidi characters and collapse whitespace (keeps web text from smuggling
    terminal escapes or right-to-left overrides into the report or the UI)."""
    t = " ".join(_CONTROL.sub("", text or "").split())
    return t[:limit] if limit else t


def md(text: str | None) -> str:
    t = clean_text(text)
    for ch in ("\\", "[", "]", "<", ">", "`", "*", "_", "#", "|"):
        t = t.replace(ch, "\\" + ch)
    return t


def md_url(url: str) -> str:
    return clean_text(url).replace(" ", "%20").replace(")", "%29").replace("(", "%28").replace("<", "%3C").replace(">", "%3E")


def _dev_block(rank, d: dict) -> list[str]:
    lines = [f"### {rank}. {md(d['title'])}", f"`{md(d['key'])}`", "", md(d["summary"]), "", "Claims:"]
    for c in d["claims"]:
        lines.append(f"- **[{LABELS[c['verdict']]}]** {md(c['text'])}  \n  Source: <{md_url(c['source_url'])}>")
        if c["verdict"] == "supported" and c.get("evidence"):
            lines.append(f"  > {md(c['evidence'])}")
    lines.append("")
    return lines


def render(*, topic: str, k: int, run_id: str, started_at: datetime, status: str, workers: dict, stats: dict,
           developments: list[dict], changes: dict | None, dropped: list[dict], stale: dict | None,
           verdict_counts: dict, notes: list[str]) -> str:
    out = [f"# {md(topic)}: top {k}", "",
           f"- Run: `{run_id}` at {started_at.strftime('%Y-%m-%d %H:%M UTC')}",
           f"- Status: **{status}**"]
    for name, w in workers.items():
        line = f"- {name.capitalize()}: {w['status']}"
        if w.get("error"):
            line += f" ({md(w['error'])})"
        out.append(line)
    out.append(f"- Budget charged: {stats['charged_steps']}/{stats['budget_steps']} model calls, "
               f"{stats['charged_fetches']}/{stats['budget_fetches']} fetches, "
               f"{stats['charged_tokens']:,}/{stats['budget_tokens']:,} tokens, {stats['searches']} searches, "
               f"{stats['duration_s']}s")
    out.append("- Verdicts: " + ", ".join(f"{verdict_counts.get(v, 0)} {v}" for v in LABELS))
    out.append("")
    for n in notes:
        out += [f"> {md(n)}", ""]

    if stale is not None:
        out += ["## No new results", "",
                "The researcher did not return new developments in this run.", ""]
        out += [f"## Last run's top {k} (STALE: from run `{stale['run_id']}`, "
                f"{stale['finished_at'].strftime('%Y-%m-%d %H:%M UTC')})", ""]
        if stale["items"]:
            out += [f"- {t['rank']}. {md(t['title'])} (`{md(t['key'])}`)" for t in stale["items"]]
        else:
            out.append("_No earlier top K to show._")
        out.append("")
    elif changes is None:
        out += [f"## Top {k}", ""]
        for i, d in enumerate(developments, 1):
            out += _dev_block(i, d)
    else:
        ranked = list(enumerate(developments, 1))
        new = [(i, d) for i, d in ranked if changes.get(d["key"]) == "new"]
        still = [(i, d) for i, d in ranked if changes.get(d["key"]) == "still"]
        out += ["## New since last run", ""] + (sum((_dev_block(i, d) for i, d in new), []) or ["_Nothing new._", ""])
        out += ["## Still in top K", ""] + (sum((_dev_block(i, d) for i, d in still), []) or ["_None._", ""])
        out += ["## Dropped", ""]
        out += [f"- {md(t['title'])} (`{md(t['key'])}`, was #{t['rank']})" for t in dropped] or ["_None._"]
        out.append("")
    if stale is None and not developments:
        out += ["_No developments in this run._", ""]
    return "\n".join(out)
