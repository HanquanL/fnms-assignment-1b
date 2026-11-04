"""Code, not the model, decides whether a supported verdict's evidence is real (PDF §9).

normalize() is exactly the PDF's four steps, in order:
    1. decode HTML entities          html.unescape
    2. Unicode NFKC                  unicodedata.normalize("NFKC", ...)
    3. collapse whitespace runs      re.sub(r"\\s+", " ", ...)
    4. lowercase                     str.lower()
Applied identically to the excerpt and to the fetched page text.

Two additions, stated in ORCHESTRATION.md:
  - the EXCERPT (not the page) has leading/trailing whitespace stripped after normalizing, so a
    trailing newline the model added can't fail an otherwise exact quote;
  - an excerpt must be at least MIN_EVIDENCE_CHARS long after normalizing. A two-word "excerpt"
    appears on almost any page and proves nothing.
"""
import html
import re
import unicodedata

MAX_EVIDENCE_CHARS = 300
MIN_EVIDENCE_CHARS = 20
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _WS.sub(" ", text)
    return text.lower()


def check_evidence(excerpt: object, page_text: str) -> tuple[bool, str]:
    """(ok, reason). ok only if the excerpt is a string of at most 300 characters (as returned,
    before normalizing), at least 20 after normalizing, and its normalized form occurs in the
    normalized page text."""
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False, "no evidence excerpt"
    if len(excerpt) > MAX_EVIDENCE_CHARS:
        return False, f"excerpt longer than {MAX_EVIDENCE_CHARS} characters"
    needle = normalize(excerpt).strip()
    if len(needle) < MIN_EVIDENCE_CHARS:
        return False, f"excerpt shorter than {MIN_EVIDENCE_CHARS} characters"
    if needle not in normalize(page_text):
        return False, "excerpt does not appear in the fetched page"
    return True, "ok"
