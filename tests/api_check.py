"""
End-to-end API check that mirrors the grading script: registers two accounts,
exercises every endpoint, and checks shapes, status codes, and the three rules.

Usage (backend running, from the repo root):
    backend\\.venv\\Scripts\\python.exe tests\\api_check.py
    backend\\.venv\\Scripts\\python.exe tests\\api_check.py --base-url http://localhost:8000
"""
import argparse
import base64
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import requests

USER_KEYS = {"id", "username", "email", "created_at", "updated_at"}
PROTECTED_CODES = (403, 404)

passed = 0
failed = 0
all_bodies: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}   {detail}")


def section(title: str) -> None:
    print(f"\n== {title}")


def show(r: requests.Response) -> str:
    return f"(got {r.status_code}: {r.text[:200]})"


def is_user(body) -> bool:
    # Exact key match: no password_hash or any other extra field allowed
    return isinstance(body, dict) and set(body.keys()) == USER_KEYS


class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def call(self, method: str, path: str, token: str | None = None, body=None, auth_header: str | None = None):
        headers = {}
        if auth_header is not None:
            headers["Authorization"] = auth_header
        elif token is not None:
            headers["Authorization"] = f"Bearer {token}"
        r = requests.request(method, self.base + path, json=body, headers=headers, timeout=20)
        all_bodies.append(r.text)
        return r


def load_jwt_secret() -> str | None:
    env = Path(__file__).resolve().parent.parent / "backend" / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("JWT_SECRET="):
            return line.split("=", 1)[1].strip()
    return None


def b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def bad_tokens(valid_token: str, user_id: str) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    claims = {"sub": user_id, "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=10)).timestamp())}

    head, payload, sig = valid_token.split(".")
    i = len(sig) // 2
    flipped = "A" if sig[i] != "A" else "B"
    tokens = {
        "garbage token": "not-a-jwt",
        "tampered signature": f"{head}.{payload}.{sig[:i]}{flipped}{sig[i + 1:]}",
        "wrong secret": jwt.encode(claims, "definitely-not-the-server-secret-0123456789", algorithm="HS256"),
        "alg=none": f"{b64url({'alg': 'none', 'typ': 'JWT'})}.{b64url(claims)}.",
    }
    secret = load_jwt_secret()
    if secret:
        expired = dict(claims, iat=int((now - timedelta(hours=2)).timestamp()),
                       exp=int((now - timedelta(hours=1)).timestamp()))
        tokens["expired token"] = jwt.encode(expired, secret, algorithm="HS256")
    else:
        print("  (skipping expired-token test: backend/.env JWT_SECRET not found)")
    return tokens


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    api = Api(parser.parse_args().base_url)

    sfx = uuid.uuid4().hex[:8]
    pw_a, pw_b, pw_a_new = "Password123!", "Password456!", "NewPassword789!"
    a = {"username": f"test_a_{sfx}", "email": f"a_{sfx}@example.com", "password": pw_a}
    b = {"username": f"test_b_{sfx}", "email": f"b_{sfx}@example.com", "password": pw_b}

    # ------------------------------------------------------------------
    section("Health")
    r = api.call("GET", "/healthz")
    check("GET /healthz -> 200 {status: ok}", r.status_code == 200 and r.json() == {"status": "ok"}, show(r))

    # ------------------------------------------------------------------
    section("Register")
    r = api.call("POST", "/api/auth/register", body=a)
    check("register A -> 201", r.status_code == 201, show(r))
    check("register A returns a user shape (no extra fields)", r.status_code == 201 and is_user(r.json()), show(r))
    a_id = r.json().get("id") if r.status_code == 201 else None

    r = api.call("POST", "/api/auth/register", body=b)
    check("register B -> 201", r.status_code == 201 and is_user(r.json()), show(r))
    b_id = r.json().get("id") if r.status_code == 201 else None

    if not (a_id and b_id):
        print("\nCannot continue without two registered users.")
        return 1

    r = api.call("POST", "/api/auth/register", body=a)
    check("duplicate username -> 409", r.status_code == 409, show(r))
    r = api.call("POST", "/api/auth/register", body={**a, "username": a["username"].upper(), "email": None})
    check("same username, different case -> 409", r.status_code == 409, show(r))
    r = api.call("POST", "/api/auth/register", body={"username": f"x_{sfx}", "password": "short1"})
    check("too-short password -> 422", r.status_code == 422, show(r))
    check("422 does not echo the submitted password", "short1" not in r.text, show(r))
    r = api.call("POST", "/api/auth/register", body={"password": "Password123!"})
    check("missing username -> 422", r.status_code == 422, show(r))

    # ------------------------------------------------------------------
    section("Login")
    r = api.call("POST", "/api/auth/login", body={"username": a["username"], "password": pw_a})
    ok = r.status_code == 200
    check("login A -> 200", ok, show(r))
    body = r.json() if ok else {}
    check("login returns access_token + token_type=bearer",
          isinstance(body.get("access_token"), str) and body.get("token_type") == "bearer", show(r))
    check("login returns a user shape", is_user(body.get("user")), show(r))
    tok_a = body.get("access_token")

    r = api.call("POST", "/api/auth/login", body={"username": b["username"], "password": pw_b})
    check("login B -> 200", r.status_code == 200, show(r))
    tok_b = r.json().get("access_token") if r.status_code == 200 else None

    if not (tok_a and tok_b):
        print("\nCannot continue without two tokens.")
        return 1

    r = api.call("POST", "/api/auth/login", body={"email": a["email"], "password": pw_a})
    check("login with email -> 200", r.status_code == 200, show(r))
    r_wrong = api.call("POST", "/api/auth/login", body={"username": a["username"], "password": "wrong-password"})
    check("wrong password -> 401", r_wrong.status_code == 401, show(r_wrong))
    r_nouser = api.call("POST", "/api/auth/login", body={"username": f"nobody_{sfx}", "password": "wrong-password"})
    check("unknown user -> 401", r_nouser.status_code == 401, show(r_nouser))
    check("wrong password and unknown user give the same body (no enumeration)",
          r_wrong.text == r_nouser.text, f"{r_wrong.text} vs {r_nouser.text}")

    # ------------------------------------------------------------------
    section("GET /api/auth/me")
    r = api.call("GET", "/api/auth/me", token=tok_a)
    check("me with A's token -> 200, returns A", r.status_code == 200 and r.json().get("id") == a_id, show(r))
    check("me returns a user shape", r.status_code == 200 and is_user(r.json()), show(r))

    # ------------------------------------------------------------------
    section("Rule 2: no / bad / expired token -> 401 on every protected route")
    protected = [
        ("GET", "/api/auth/me", None),
        ("GET", f"/api/users/{a_id}", None),
        ("PATCH", f"/api/users/{a_id}", {"email": f"rule2_{sfx}@example.com"}),
        ("DELETE", f"/api/users/{a_id}", None),
    ]
    variants = {"no Authorization header": None, "wrong scheme (Basic)": "Basic dXNlcjpwYXNz"}
    variants.update({name: f"Bearer {t}" for name, t in bad_tokens(tok_a, a_id).items()})
    for method, path, payload in protected:
        for name, header in variants.items():
            r = api.call(method, path, body=payload, auth_header=header) if header else api.call(method, path, body=payload)
            check(f"{method} {path.replace(a_id, ':id')} with {name} -> 401", r.status_code == 401, show(r))

    # ------------------------------------------------------------------
    section("Rule 3: A's token against B's :id")
    r_get = api.call("GET", f"/api/users/{b_id}", token=tok_a)
    r_patch = api.call("PATCH", f"/api/users/{b_id}", token=tok_a, body={"email": f"hacked_{sfx}@example.com"})
    r_del = api.call("DELETE", f"/api/users/{b_id}", token=tok_a)
    codes = [r_get.status_code, r_patch.status_code, r_del.status_code]
    check("GET other user refused (403/404)", r_get.status_code in PROTECTED_CODES, show(r_get))
    check("PATCH other user refused (403/404)", r_patch.status_code in PROTECTED_CODES, show(r_patch))
    check("DELETE other user refused (403/404)", r_del.status_code in PROTECTED_CODES, show(r_del))
    check(f"all three use the same code {codes}", len(set(codes)) == 1)
    code = codes[0]

    r = api.call("GET", f"/api/users/{uuid.uuid4()}", token=tok_a)
    check(f"nonexistent id -> same code ({code})", r.status_code == code, show(r))
    r = api.call("GET", "/api/users/not-a-uuid", token=tok_a)
    check(f"malformed id -> same code ({code})", r.status_code == code, show(r))

    r = api.call("GET", f"/api/users/{b_id}", token=tok_b)
    check("B is untouched after A's attempts", r.status_code == 200 and r.json().get("email") == b["email"], show(r))

    # ------------------------------------------------------------------
    section("Own account: GET / PATCH")
    r = api.call("GET", f"/api/users/{a_id}", token=tok_a)
    check("GET own user -> 200 user shape", r.status_code == 200 and is_user(r.json()), show(r))

    new_email = f"a2_{sfx}@example.com"
    r = api.call("PATCH", f"/api/users/{a_id}", token=tok_a, body={"email": new_email})
    check("PATCH own email -> 200 and updated",
          r.status_code == 200 and is_user(r.json()) and r.json().get("email") == new_email, show(r))

    r = api.call("PATCH", f"/api/users/{a_id}", token=tok_a,
                 body={"id": str(uuid.uuid4()), "password_hash": "x", "created_at": "2000-01-01T00:00:00Z"})
    check("PATCH with protected fields is ignored (no mass assignment)",
          r.status_code == 200 and r.json().get("id") == a_id, show(r))

    r = api.call("PATCH", f"/api/users/{a_id}", token=tok_a, body={"email": b["email"]})
    check("PATCH to an email already in use -> 409", r.status_code == 409, show(r))

    r = api.call("PATCH", f"/api/users/{a_id}", token=tok_a, body={"password": pw_a_new})
    check("PATCH password without current_password -> 400", r.status_code == 400, show(r))
    r = api.call("PATCH", f"/api/users/{a_id}", token=tok_a, body={"password": pw_a_new, "current_password": pw_a})
    check("PATCH password with current_password -> 200", r.status_code == 200 and is_user(r.json()), show(r))
    r = api.call("POST", "/api/auth/login", body={"username": a["username"], "password": pw_a_new})
    check("login with new password -> 200", r.status_code == 200, show(r))
    r = api.call("POST", "/api/auth/login", body={"username": a["username"], "password": pw_a})
    check("login with old password -> 401", r.status_code == 401, show(r))

    # ------------------------------------------------------------------
    section("DELETE own account")
    r = api.call("DELETE", f"/api/users/{b_id}", token=tok_b)
    check("B deletes own account -> 204", r.status_code == 204, show(r))
    r = api.call("GET", "/api/auth/me", token=tok_b)
    check("B's token after deletion -> 401", r.status_code == 401, show(r))
    r = api.call("POST", "/api/auth/login", body={"username": b["username"], "password": pw_b})
    check("B can no longer log in -> 401", r.status_code == 401, show(r))
    r = api.call("DELETE", f"/api/users/{a_id}", token=tok_a)
    check("A deletes own account -> 204 (cleanup)", r.status_code == 204, show(r))

    # ------------------------------------------------------------------
    section("Rule 1: no password hash (or plaintext password) in ANY response")
    leaks = [t for t in all_bodies if "$argon2" in t or "password_hash" in t]
    check(f"none of {len(all_bodies)} response bodies contain a hash", not leaks, leaks[:1])
    echoed = [t for t in all_bodies if any(p in t for p in (pw_a, pw_b, pw_a_new))]
    check("no response echoes a submitted password", not echoed, echoed[:1])

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())