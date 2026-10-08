"""URL canonicalization: one spelling per article, so 'already seen' works.

https://Example.com:443/a/?utm_source=x&b=2&a=1#top  ->  https://example.com/a?a=1&b=2
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref_src", "ref_url", "cmpid", "ncid", "guccounter",
}


def canonicalize(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    port = parts.port
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    if ":" in host and not host.startswith("["):  # IPv6 literal
        netloc = f"[{host}]" + ("" if netloc == host else netloc[len(host):])

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))