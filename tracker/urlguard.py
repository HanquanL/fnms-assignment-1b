"""Guardrails for fetch_article.

Every check here runs BEFORE any request is made:
  - scheme must be http(s); no credentials in the URL; only allowed ports
  - the host must pass the allow/deny lists in config.yaml
  - EVERY address the host resolves to must be public: no loopback, private,
    link-local (169.254.169.254 = cloud metadata), multicast, reserved, ...
  - the connection is then pinned to the address we checked, so DNS can't
    answer differently a moment later (DNS rebinding)
Redirects are not followed automatically; each hop goes through vet_url again.
"""
import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


class UrlRejected(Exception):
    """A guardrail refused the URL. No request was made."""


class FetchFailed(Exception):
    """The URL was allowed but fetching it didn't work (DNS, timeout, HTTP error, too big...)."""


@dataclass(frozen=True)
class FetchPolicy:
    allowed_schemes: tuple[str, ...]
    allowed_ports: tuple[int, ...]
    allowed_hosts: tuple[str, ...]
    blocked_hosts: tuple[str, ...]
    timeout_seconds: float
    total_timeout_seconds: float
    max_bytes: int
    max_redirects: int
    max_chars_to_model: int

    @classmethod
    def from_config(cls, fetch: Mapping) -> "FetchPolicy":
        return cls(
            allowed_schemes=tuple(s.lower() for s in fetch["allowed_schemes"]),
            allowed_ports=tuple(int(p) for p in fetch["allowed_ports"]),
            allowed_hosts=tuple(h.lower() for h in fetch["allowed_hosts"]),
            blocked_hosts=tuple(h.lower() for h in fetch["blocked_hosts"]),
            timeout_seconds=float(fetch["timeout_seconds"]),
            total_timeout_seconds=float(fetch["total_timeout_seconds"]),
            max_bytes=int(fetch["max_bytes"]),
            max_redirects=int(fetch["max_redirects"]),
            max_chars_to_model=int(fetch["max_chars_to_model"]),
        )


@dataclass(frozen=True)
class VettedTarget:
    """A URL that passed every check, plus the exact IP we will connect to."""
    url: str
    scheme: str
    host: str
    port: int
    path: str
    ip: str


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    return any(p == "*" or host == p or host.endswith("." + p) for p in patterns)


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the odd IPv4 spellings some resolvers accept: 2130706433,
    0x7f.1, 127.1, 0177.0.0.1. Done here so the result doesn't depend on
    the OS resolver (Linux accepts these, Windows doesn't)."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        nums = [int(p, 0) if not (len(p) > 1 and p[0] == "0" and p[1] not in "xX") else int(p, 8)
                for p in parts]
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
    """IPv6 addresses that really point at an IPv4 address."""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped                       # ::ffff:127.0.0.1
    if ip.sixtofour:
        return ip.sixtofour                         # 2002::/16
    if ip.teredo:
        return ip.teredo[1]                         # 2001::/32 (client address)
    if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 well-known prefix
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def forbidden_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why we refuse to connect to this address, or None if it's public."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None:
            reason = forbidden_reason(inner)
            if reason:
                return f"{reason} (embedded in {ip})"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "private"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_reserved:
        return "reserved"
    if not ip.is_global:
        return "non-public"
    return None


Resolver = Callable[..., list]


def vet_url(url: str, policy: FetchPolicy, resolver: Resolver = socket.getaddrinfo) -> VettedTarget:
    """Raise UrlRejected unless the URL is safe to fetch. Never makes an HTTP request."""
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UrlRejected(f"URL longer than {MAX_URL_LENGTH} characters")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in policy.allowed_schemes:
        raise UrlRejected(f"scheme '{scheme or '(none)'}' is not allowed")
    if parts.username is not None or parts.password is not None:
        raise UrlRejected("credentials in the URL are not allowed")

    host = parts.hostname  # lowercased, IPv6 brackets removed
    if not host:
        raise UrlRejected("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UrlRejected("invalid port")
    if port not in policy.allowed_ports:
        raise UrlRejected(f"port {port} is not allowed")

    try:
        host = host.rstrip(".").encode("idna").decode("ascii")  # internationalized names -> punycode
    except UnicodeError:
        raise UrlRejected("invalid hostname")
    if _host_matches(host, policy.blocked_hosts):
        raise UrlRejected(f"host '{host}' is blocked by config")
    if not _host_matches(host, policy.allowed_hosts):
        raise UrlRejected(f"host '{host}' is not in allowed_hosts")

    # Literal IPs (any spelling) are checked directly; names are resolved first.
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = _legacy_ipv4(host)

    if literal is not None:
        addresses = [literal]
    else:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as e:
            raise FetchFailed(f"DNS lookup failed for '{host}'") from e
        addresses = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if ip not in addresses:
                addresses.append(ip)
        if not addresses:
            raise FetchFailed(f"'{host}' has no addresses")

    # Every address must be public: a name with one public and one private
    # address could otherwise be steered to the private one.
    for ip in addresses:
        reason = forbidden_reason(ip)
        if reason:
            raise UrlRejected(f"host '{host}' resolves to a {reason} address ({ip})")

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return VettedTarget(url=url, scheme=scheme, host=host, port=port, path=path, ip=str(addresses[0]))