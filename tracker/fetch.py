"""fetch_article: download one web page safely and return its text.

Order of operations for every hop (the first URL and each redirect):
  vet_url (no network except DNS) -> connect to the vetted IP only ->
  stream the body with a byte cap and a total deadline -> extract text.
"""
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx

from tracker.extract import html_to_text, plain_text
from tracker.urlguard import FetchFailed, FetchPolicy, UrlRejected, VettedTarget, vet_url
from tracker.urls import canonicalize

USER_AGENT = "FNMS-Tracker/1.0 (+https://github.com/HanquanL/fnms-assignment-1b; NYU course project)"
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


@dataclass
class FetchResult:
    status: str                  # fetched | rejected | failed
    url: str
    canonical_url: str
    final_url: str | None = None
    title: str | None = None
    text: str = ""
    http_status: int | None = None
    reason: str | None = None
    fetched_at: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _pinned_request(client: httpx.Client, t: VettedTarget) -> httpx.Request:
    """Build a request to the vetted IP, while still presenting the real
    hostname (Host header, and SNI so the TLS certificate is checked for it)."""
    ip = f"[{t.ip}]" if ":" in t.ip else t.ip
    default = 443 if t.scheme == "https" else 80
    netloc = ip if t.port == default else f"{ip}:{t.port}"
    host_header = t.host if t.port == default else f"{t.host}:{t.port}"
    extensions = {"sni_hostname": t.host} if t.scheme == "https" else {}
    return client.build_request(
        "GET", f"{t.scheme}://{netloc}{t.path}",
        headers={"Host": host_header, "User-Agent": USER_AGENT,
                 "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9"},
        extensions=extensions,
    )


def _read_capped(resp: httpx.Response, policy: FetchPolicy, deadline: float) -> bytes:
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > policy.max_bytes:
        raise FetchFailed(f"response is {int(declared):,} bytes, over the {policy.max_bytes:,} byte limit")
    body = bytearray()
    for chunk in resp.iter_bytes():
        body += chunk
        if len(body) > policy.max_bytes:
            raise FetchFailed(f"response exceeded the {policy.max_bytes:,} byte limit")
        if time.monotonic() > deadline:  # a server dripping bytes can't hold us forever
            raise FetchFailed(f"download took longer than {policy.total_timeout_seconds:g}s")
    return bytes(body)


def fetch_article(url: str, policy: FetchPolicy) -> FetchResult:
    started = time.monotonic()
    deadline = started + policy.total_timeout_seconds
    result = FetchResult(status="failed", url=url, canonical_url=canonicalize(url) if url else url,
                         fetched_at=datetime.now(timezone.utc).isoformat())

    try:
        current = url
        # verify=True + sni_hostname: certificates are checked against the real name
        with httpx.Client(timeout=policy.timeout_seconds, follow_redirects=False, verify=True) as client:
            for _hop in range(policy.max_redirects + 1):
                target = vet_url(current, policy)          # raises UrlRejected / FetchFailed
                resp = client.send(_pinned_request(client, target), stream=True)
                try:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchFailed(f"HTTP {resp.status_code} redirect without a Location")
                        current = urljoin(current, location)  # next loop vets it again
                        continue

                    result.http_status = resp.status_code
                    result.final_url = current
                    if resp.status_code >= 400:
                        raise FetchFailed(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and ctype not in TEXT_TYPES:
                        raise FetchFailed(f"unsupported content type '{ctype}'")

                    body = _read_capped(resp, policy, deadline)
                    decoded = body.decode(resp.encoding or "utf-8", errors="replace")
                    title, text = plain_text(decoded) if ctype == "text/plain" else html_to_text(decoded)
                    result.status, result.title, result.text = "fetched", title, text
                    if not text:
                        result.status, result.reason = "failed", "no readable text on the page"
                    return result
                finally:
                    resp.close()
            raise FetchFailed(f"more than {policy.max_redirects} redirects")

    except UrlRejected as e:
        result.status, result.reason = "rejected", str(e)
    except FetchFailed as e:
        result.status, result.reason = "failed", str(e)
    except httpx.TimeoutException:
        result.status, result.reason = "failed", f"timed out after {policy.timeout_seconds:g}s"
    except httpx.HTTPError as e:
        result.status, result.reason = "failed", f"{type(e).__name__}: {e}"[:300]
    finally:
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result