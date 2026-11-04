"""The verifier's own fetch_article, with the A1B guardrails (PDF §10).

A deliberate copy of tracker/urlguard.py + tracker/fetch.py + tracker/extract.py (DECISIONS.md D3):
the verifier is deployed alone and must not import the researcher's code. Changes from the copy:
  - `on_hop(url)` is called before EVERY outgoing request (the first URL and each redirect) and can
    refuse it: that is how one redirect hop = one fetch from the budget (PDF §3);
  - a `deadline` (monotonic seconds): no hop starts after it, and per-hop timeouts shrink to fit;
  - injectable `resolver` and `transport`, so tests run with no network at all.

Every hop: vet_url (DNS only, every resolved address must be public) -> on_hop -> connect to the
vetted IP only (Host header + TLS SNI keep the real name, so certificates are still checked) ->
stream the body with a byte cap and a total deadline -> extract text.
"""
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

MAX_URL_LENGTH = 2048
USER_AGENT = "FNMS-Verifier/1.0 (NYU course project; claim checking)"
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class UrlRejected(Exception):
    """A guardrail refused the URL. No request was made."""


class FetchFailed(Exception):
    """The URL was allowed but fetching it didn't work."""


class HopRefused(Exception):
    """on_hop refused the next request (fetch budget used up). No request was made."""


class DeadlineReached(Exception):
    """The request's time budget ran out before the next hop could start."""


@dataclass(frozen=True)
class FetchPolicy:
    allowed_schemes: tuple[str, ...] = ("http", "https")
    allowed_ports: tuple[int, ...] = (80, 443)
    timeout_seconds: float = 10.0
    total_timeout_seconds: float = 20.0
    max_bytes: int = 2_000_000
    max_redirects: int = 5

    @classmethod
    def from_config(cls, fetch) -> "FetchPolicy":
        return cls(
            allowed_schemes=tuple(s.lower() for s in fetch["allowed_schemes"]),
            allowed_ports=tuple(int(p) for p in fetch["allowed_ports"]),
            timeout_seconds=float(fetch["timeout_seconds"]),
            total_timeout_seconds=float(fetch["total_timeout_seconds"]),
            max_bytes=int(fetch["max_bytes"]),
            max_redirects=int(fetch["max_redirects"]),
        )


@dataclass(frozen=True)
class VettedTarget:
    url: str
    scheme: str
    host: str
    port: int
    path: str
    ip: str


# ------------------------------------------------------------- addresses ----

def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Odd IPv4 spellings some resolvers accept: 2130706433, 0x7f.1, 127.1, 0177.0.0.1."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        nums = [int(p, 0) if not (len(p) > 1 and p[0] == "0" and p[1] not in "xX") else int(p, 8) for p in parts]
    except ValueError:
        return None
    *head, last = nums
    if any(not 0 <= n <= 255 for n in head) or last < 0 or last >= 256 ** (5 - len(nums)):
        return None
    value = 0
    for n in head:
        value = value * 256 + n
    value = value * 256 ** (5 - len(nums)) + last
    return ipaddress.IPv4Address(value) if value < 2**32 else None


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def forbidden_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why we refuse this address, or None if it is public. Covers loopback (127/8, ::1), private
    (10/8, 172.16/12, 192.168/16, fc00::/7), link-local (169.254/16 incl. the 169.254.169.254
    metadata address, fe80::/10), unspecified (0.0.0.0, ::), multicast, reserved, and IPv4
    hidden inside IPv6."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None:
            reason = forbidden_reason(inner)
            if reason:
                return f"{reason} (embedded in {ip})"
    for attr, name in (("is_loopback", "loopback"), ("is_link_local", "link-local"), ("is_private", "private"),
                       ("is_multicast", "multicast"), ("is_unspecified", "unspecified"),
                       ("is_reserved", "reserved")):
        if getattr(ip, attr):
            return name
    if not ip.is_global:
        return "non-public"
    return None


Resolver = Callable[..., list]


def vet_url(url: str, policy: FetchPolicy, resolver: Resolver = socket.getaddrinfo) -> VettedTarget:
    """Raise UrlRejected unless the URL is safe to fetch. Makes no HTTP request (DNS only)."""
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UrlRejected(f"URL longer than {MAX_URL_LENGTH} characters")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in policy.allowed_schemes:
        raise UrlRejected(f"scheme '{scheme or '(none)'}' is not allowed")
    if parts.username is not None or parts.password is not None:
        raise UrlRejected("credentials in the URL are not allowed")
    host = parts.hostname
    if not host:
        raise UrlRejected("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UrlRejected("invalid port")
    if port not in policy.allowed_ports:
        raise UrlRejected(f"port {port} is not allowed")
    try:
        host = host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise UrlRejected("invalid hostname")
    if host == "localhost" or host.endswith(".localhost"):
        raise UrlRejected("host 'localhost' is not allowed")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = _legacy_ipv4(host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError) as e:
            raise FetchFailed(f"DNS lookup failed for '{host}'") from e
        addresses = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if ip not in addresses:
                addresses.append(ip)
        if not addresses:
            raise FetchFailed(f"'{host}' has no addresses")
    for ip in addresses:   # every address must be public, not just the first
        reason = forbidden_reason(ip)
        if reason:
            raise UrlRejected(f"host '{host}' resolves to a {reason} address ({ip})")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return VettedTarget(url=url, scheme=scheme, host=host, port=port, path=path, ip=str(addresses[0]))


# ------------------------------------------------------------------ text ----

DROP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "object", "embed",
             "form", "nav", "footer", "header", "aside", "button", "select"]
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote", "td", "figcaption"]
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
_SPACES = re.compile(r"[ \t ]+")


def clean(text: str) -> str:
    return _SPACES.sub(" ", _CONTROL.sub("", text)).strip()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines: list[str] = []
    for el in root.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):
            continue
        line = clean(el.get_text(" "))
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    text = "\n".join(lines)
    if len(text) < 200:
        text = "\n".join(filter(None, (clean(t) for t in root.get_text("\n").splitlines())))
    return text


# ----------------------------------------------------------------- fetch ----

@dataclass
class FetchResult:
    status: str                 # fetched | rejected | failed
    url: str
    final_url: str | None = None
    text: str = ""
    http_status: int | None = None
    reason: str | None = None
    hops: int = 0               # requests actually sent
    elapsed_ms: int = 0


def _pinned_request(client: httpx.Client, t: VettedTarget) -> httpx.Request:
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
        if time.monotonic() > deadline:
            raise FetchFailed("download took longer than the time allowed")
    return bytes(body)


def fetch_article(url: str, policy: FetchPolicy, *, on_hop: Callable[[str], bool] | None = None,
                  deadline: float | None = None, resolver: Resolver = socket.getaddrinfo,
                  transport: httpx.BaseTransport | None = None) -> FetchResult:
    """Fetch one page. Raises HopRefused / DeadlineReached (budget, not the page's fault); every
    other problem is a FetchResult with status rejected (guardrail, nothing sent) or failed."""
    started = time.monotonic()
    stop = started + policy.total_timeout_seconds
    if deadline is not None:
        stop = min(stop, deadline)
    result = FetchResult(status="failed", url=url)
    try:
        current = url
        with httpx.Client(timeout=policy.timeout_seconds, follow_redirects=False, verify=True,
                          transport=transport) as client:
            for _hop in range(policy.max_redirects + 1):
                target = vet_url(current, policy, resolver)   # every hop is vetted again
                remaining = stop - time.monotonic()
                if deadline is not None and time.monotonic() >= deadline:
                    raise DeadlineReached("time budget used up before the next request")
                if remaining <= 0.5:
                    raise FetchFailed("timed out (total time limit for this page)")
                if on_hop is not None and not on_hop(current):
                    raise HopRefused("fetch budget used up")
                result.hops += 1
                t = min(policy.timeout_seconds, remaining)
                request = _pinned_request(client, target)
                request.extensions["timeout"] = httpx.Timeout(t).as_dict()
                resp = client.send(request, stream=True)
                try:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchFailed(f"HTTP {resp.status_code} redirect without a Location")
                        current = urljoin(current, location)
                        continue
                    result.http_status, result.final_url = resp.status_code, current
                    if resp.status_code >= 400:
                        raise FetchFailed(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and ctype not in TEXT_TYPES:
                        raise FetchFailed(f"unsupported content type '{ctype}'")
                    body = _read_capped(resp, policy, stop)
                    decoded = body.decode(resp.encoding or "utf-8", errors="replace")
                    text = ("\n".join(filter(None, (clean(t) for t in decoded.splitlines())))
                            if ctype == "text/plain" else html_to_text(decoded))
                    if not text:
                        raise FetchFailed("no readable text on the page")
                    result.status, result.text = "fetched", text
                    return result
                finally:
                    resp.close()
            raise FetchFailed(f"more than {policy.max_redirects} redirects")
    except UrlRejected as e:
        result.status, result.reason = "rejected", str(e)
    except FetchFailed as e:
        result.status, result.reason = "failed", str(e)
    except httpx.TimeoutException:
        result.status, result.reason = "failed", "timed out"
    except httpx.HTTPError as e:
        result.status, result.reason = "failed", f"{type(e).__name__}: {e}"[:300]
    finally:
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result
