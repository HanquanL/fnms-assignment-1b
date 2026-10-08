"""The tracker as a client of the A1 backend (requirement 6 via the optional
"store it in your A1 database" route). The tracker never touches the database:
it logs in like any user, gets a JWT, and calls /api/tracker/*. So everything
A1 enforces -- 401 without a valid token, one user can't see another's data --
applies to the tracker too.
"""
from typing import Any

import httpx

from tracker.errors import RetryPolicy, TerminalError, TransientError, call_with_retry


class BackendClient:
    def __init__(self, base_url: str, username: str, password: str, retry: RetryPolicy, *,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None, sleep=None):
        self.base = base_url.rstrip("/")
        self._username, self._password = username, password
        self._retry = retry
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._token: str | None = None
        self._sleep = sleep

    # ---------- plumbing ----------

    def _once(self, method: str, path: str, *, auth: bool = True, json: Any = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
        try:
            resp = self._client.request(method, self.base + path, json=json, headers=headers)
        except httpx.ConnectError as e:
            # Nothing listening: retrying won't start the server
            raise TerminalError(f"can't reach the backend at {self.base}. Is it running? "
                                f"(cd backend; uvicorn app.main:app --port 8000)") from e
        except httpx.TimeoutException as e:
            raise TransientError(f"backend timed out ({path})") from e
        except httpx.TransportError as e:
            raise TransientError(f"network error talking to the backend: {type(e).__name__}") from e
        if resp.status_code >= 500:  # e.g. Neon waking up from idle
            raise TransientError(f"backend error {resp.status_code} on {path}")
        return resp

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        opts = {"sleep": self._sleep} if self._sleep else {}
        return call_with_retry(lambda: self._once(method, path, **kw), self._retry,
                               what=f"backend {method} {path}", **opts)

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            return str(resp.json().get("detail"))[:300]
        except ValueError:
            return resp.text[:300]

    # ---------- API ----------

    def login(self) -> None:
        resp = self._call("POST", "/api/auth/login", auth=False,
                          json={"username": self._username, "password": self._password})
        if resp.status_code == 401:
            raise TerminalError(f"the backend rejected TRACKER_USERNAME/TRACKER_PASSWORD for "
                                f"'{self._username}'. Check .env (and run `python seed.py` in backend/).")
        if resp.status_code != 200:
            raise TerminalError(f"login failed ({resp.status_code}): {self._detail(resp)}")
        self._token = resp.json()["access_token"]

    def _authed(self, method: str, path: str, json: Any = None) -> httpx.Response:
        if self._token is None:
            self.login()
        resp = self._call(method, path, json=json)
        if resp.status_code == 401:  # token expired mid-run: log in again once
            self.login()
            resp = self._call(method, path, json=json)
        return resp

    def load_state(self) -> dict:
        resp = self._authed("GET", "/api/tracker/state")
        if resp.status_code != 200:
            raise TerminalError(f"loading tracker state failed ({resp.status_code}): {self._detail(resp)}")
        return resp.json()

    def save_run(self, payload: dict) -> dict:
        resp = self._authed("POST", "/api/tracker/runs", json=payload)
        if resp.status_code != 201:
            raise TerminalError(f"saving the run failed ({resp.status_code}): {self._detail(resp)}")
        return resp.json()

    def reset(self) -> None:
        resp = self._authed("DELETE", "/api/tracker/state")
        if resp.status_code != 204:
            raise TerminalError(f"reset failed ({resp.status_code}): {self._detail(resp)}")

    def close(self) -> None:
        self._client.close()
