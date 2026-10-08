"""HTML -> (title, plain text). Standard-library parser via BeautifulSoup.

Nothing here executes the page: scripts, styles, and iframes are deleted, not
run. Control characters are stripped so web text can't inject terminal escape
sequences when the CLI prints it.
"""
import re

from bs4 import BeautifulSoup

DROP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "object", "embed",
             "form", "nav", "footer", "header", "aside", "button", "select"]
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote", "td", "figcaption"]
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_SPACES = re.compile(r"[ \t\u00a0]+")


def clean(text: str) -> str:
    return _SPACES.sub(" ", _CONTROL.sub("", text)).strip()


def html_to_text(html: str) -> tuple[str | None, str]:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"]
    elif soup.title and soup.title.string:
        title = soup.title.string
    elif soup.h1:
        title = soup.h1.get_text(" ")
    title = clean(title)[:500] if title else None

    for tag in soup(DROP_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup

    lines: list[str] = []
    for el in root.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):  # keep only innermost blocks, so nested text isn't repeated
            continue
        line = clean(el.get_text(" "))
        if line and (not lines or lines[-1] != line):
            lines.append(line)

    text = "\n".join(lines)
    if len(text) < 200:  # pages without semantic tags: fall back to all visible text
        text = "\n".join(filter(None, (clean(t) for t in root.get_text("\n").splitlines())))
    return title, text


def plain_text(body: str) -> tuple[None, str]:
    return None, "\n".join(filter(None, (clean(t) for t in body.splitlines())))