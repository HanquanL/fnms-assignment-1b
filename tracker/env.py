"""Secrets come from environment variables, optionally loaded from the
git-ignored repo-root .env (requirement 14). Values are never printed."""
import os

from tracker.config import ROOT
from tracker.errors import TerminalError

_loaded = False


def _load_dotenv() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            # A variable already set in the real environment wins over the file
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def secret(name: str) -> str:
    _load_dotenv()
    value = os.environ.get(name, "").strip()
    if not value:
        raise TerminalError(f"{name} is not set. Add it to {ROOT / '.env'} (see .env.example).")
    return value