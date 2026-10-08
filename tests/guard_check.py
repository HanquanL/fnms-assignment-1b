"""
Checks for fetch_article's guardrails (requirement 8). Most cases never touch
the network: vet_url only does DNS, and a fake resolver covers DNS tricks.

Usage (from the repo root, tracker venv):
    .venv\\Scripts\\python.exe tests\\guard_check.py
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.config import load_config  # noqa: E402
from tracker.fetch import fetch_article  # noqa: E402
from tracker.urlguard import FetchFailed, FetchPolicy, UrlRejected, vet_url  # noqa: E402

policy = FetchPolicy.from_config(load_config()["fetch"])
passed = failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    passed, failed = (passed + 1, failed) if cond else (passed, failed + 1)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))


def rejected(url: str, resolver=socket.getaddrinfo) -> tuple[bool, str]:
    try:
        vet_url(url, policy, resolver)
        return False, "allowed"
    except UrlRejected as e:
        return True, str(e)
    except FetchFailed as e:
        return False, f"failed instead of rejected: {e}"


def fake_resolver(*ips: str):
    def resolve(host, port, type=0):
        fam = lambda ip: socket.AF_INET6 if ":" in ip else socket.AF_INET  # noqa: E731
        return [(fam(ip), socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return resolve


print("\n== Rejected before any request")
for url in [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "example.com/no-scheme",
    "http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://0.0.0.0/",
    "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/", "http://100.64.0.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/", "http://[fd00::1]/",
    "http://2130706433/", "http://0x7f.1/", "http://127.1/", "http://0177.0.0.1/",
    "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::a9fe:a9fe]/",
    "http://user:pass@example.com/", "http://example.com:8080/", "http://example.com:22/",
    "http://" + "a" * 2100 + ".com/",
]:
    ok, why = rejected(url)
    check(f"{url[:60]:<60} {why[:70]}", ok, why)

print("\n== DNS tricks (fake resolver)")
ok, why = rejected("http://evil.example/", fake_resolver("127.0.0.1"))
check("name resolving to loopback", ok, why)
ok, why = rejected("http://evil.example/", fake_resolver("93.184.216.34", "10.0.0.5"))
check("name with one public AND one private address", ok, why)
ok, why = rejected("http://evil.example/", fake_resolver("169.254.169.254"))
check("name resolving to the cloud metadata address", ok, why)
ok, why = rejected("http://fine.example/", fake_resolver("93.184.216.34"))
check("name resolving only to a public address is allowed", not ok, why)

print("\n== Real requests (network)")
r = fetch_article("https://example.com", policy)
check("public page is fetched", r.status == "fetched" and "Example Domain" in (r.title or ""), r.reason or "")
r = fetch_article("http://httpbin.org/redirect-to?url=http://127.0.0.1/", policy)
check("redirect to loopback is rejected at the redirect hop", r.status == "rejected", f"{r.status}: {r.reason}")
r = fetch_article("https://wrong.host.badssl.com/", policy)
check("TLS certificate is checked against the real hostname", r.status == "failed" and "SSL" in (r.reason or ""),
      f"{r.status}: {r.reason}")
r = fetch_article("https://httpbin.org/image/png", policy)
check("non-text content type is refused", r.status == "failed" and "content type" in (r.reason or ""), r.reason or "")

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)