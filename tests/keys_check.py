import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    # Real environment variables
    for key in ("GEMINI_API_KEY", "TAVILY_API_KEY", "GEMINI_MODEL"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env

def call(method: str, url: str, headers: dict, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, None
    except urllib.error.URLError as e:
        return None, {"error": str(e.reason)}

def check_gemini(key: str, model: str) -> bool:
    print("\n== Gemini")
    # Key goes in a header, not the URL
    headers = {"x-goog-api-key": key}

    status, body = call("GET", f"{GEMINI_BASE}/models?pageSize=200", headers)
    if status != 200:
        print(f"  FAIL  list models -> {status}: {json.dumps(body)[:400]}")
        return False
    names = [
        m["name"].removeprefix("models/")
        for m in body.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    print(f"  OK    key works; {len(names)} models support generateContent. Flash-family models:")
    for name in sorted(n for n in names if "flash" in n):
        print(f"          {name}")

    status, body = call(
        "POST", f"{GEMINI_BASE}/models/{model}:generateContent", headers,
        {"contents": [{"role": "user", "parts": [{"text": "Reply with exactly: OK"}]}]},
    )
    if status != 200:
        print(f"  FAIL  generateContent on {model} -> {status}: {json.dumps(body)[:400]}")
        return False
    text = body["candidates"][0]["content"]["parts"][0]["text"].strip()
    print(f"  OK    {model} replied {text!r}; usage = {body.get('usageMetadata')}")
    return True

def check_tavily(key: str) -> bool:
    print("\n== Tavily")
    status, body = call(
        "POST", "https://api.tavily.com/search", {"Authorization": f"Bearer {key}"},
        {"query": "open-weight model release", "max_results": 3, "search_depth": "basic"},
    )
    if status != 200:
        print(f"  FAIL  search -> {status}: {json.dumps(body)[:400]}")
        return False
    print(f"  OK    search returned {len(body.get('results', []))} results:")
    for r in body.get("results", []):
        print(f"          {r.get('title', '')[:70]}  ({r.get('url')})")
    return True


def main() -> int:
    env = load_env()
    ok = True
    for name in ("GEMINI_API_KEY", "TAVILY_API_KEY"):
        if not env.get(name):
            print(f"Missing {name} (set it in {ROOT / '.env'})")
            ok = False
    if not ok:
        return 1
    ok = check_gemini(env["GEMINI_API_KEY"], env.get("GEMINI_MODEL", "gemini-3.8-flash"))
    ok = check_tavily(env["TAVILY_API_KEY"]) and ok
    print("\nAll keys OK." if ok else "\nSome checks failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())