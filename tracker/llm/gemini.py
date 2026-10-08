"""Gemini adapter: REST generateContent via httpx (no SDK, so no hidden retries).

Error classification is the interesting part. Gemini answers BOTH "too many
requests this minute" and "no more requests today" with 429 RESOURCE_EXHAUSTED;
the difference is only in the error details (QuotaFailure.violations[].quotaId
contains PerMinute or PerDay). And a bad API key is a 400 INVALID_ARGUMENT with
reason API_KEY_INVALID, not a 401/403 -- found by testing, not from the docs.
"""
import re

import httpx

from tracker.errors import TerminalError, TransientError
from tracker.llm.base import HistoryItem, ModelTurn, ToolCall, ToolResults, ToolSpec, Usage, UserText

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _retry_delay(details: list[dict]) -> float | None:
    for d in details:
        if d.get("@type", "").endswith("RetryInfo"):
            m = re.match(r"^([\d.]+)s$", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def classify_gemini(resp: httpx.Response) -> Exception:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    code, status = resp.status_code, err.get("status", "")
    msg = str(err.get("message", resp.text))[:300]
    details = err.get("details") or []
    reasons = {d.get("reason") for d in details if d.get("@type", "").endswith("ErrorInfo")}
    quota_ids = [v.get("quotaId", "") for d in details if d.get("@type", "").endswith("QuotaFailure")
                 for v in d.get("violations", [])]

    if code == 429:
        daily = [q for q in quota_ids if "PerDay" in q]
        if daily:
            return TerminalError(
                f"Gemini daily quota used up ({daily[0]}). It resets at midnight Pacific time; "
                f"retrying today won't help.")
        which = next((q for q in quota_ids if "PerMinute" in q), "rate limit")
        return TransientError(f"Gemini per-minute limit (429, {which})", retry_after=_retry_delay(details))
    if code in (500, 502, 503, 504):
        return TransientError(f"Gemini server error ({code} {status}): {msg}")
    if "API_KEY_INVALID" in reasons or code in (401, 403):
        return TerminalError(f"Gemini rejected the API key ({code} {status}). Check GEMINI_API_KEY in .env.")
    if code == 402:
        return TerminalError(f"Gemini says payment is required (402): {msg}")
    if code == 404:
        return TerminalError(f"Gemini model not found (404): {msg} -- check model.name in config.yaml")
    return TerminalError(f"Gemini rejected the request ({code} {status}): {msg}")


class GeminiClient:
    provider = "gemini"

    def __init__(self, model: str, api_key: str, *, temperature: float = 0.2, max_output_tokens: int = 8192,
                 timeout: float = 90.0, transport: httpx.BaseTransport | None = None):
        self.model = model
        self._key = api_key
        self._gen = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
        self._timeout = timeout
        self._transport = transport

    @staticmethod
    def _contents(history: list[HistoryItem]) -> list[dict]:
        out = []
        for item in history:
            if isinstance(item, UserText):
                out.append({"role": "user", "parts": [{"text": item.text}]})
            elif isinstance(item, ModelTurn):
                out.append({"role": "model", "parts": item.raw})  # verbatim: keeps ids/thought signatures
            elif isinstance(item, ToolResults):
                parts = []
                for r in item.results:
                    fr = {"name": r.call.name, "response": r.content}
                    if r.call.id:
                        fr["id"] = r.call.id
                    parts.append({"functionResponse": fr})
                out.append({"role": "user", "parts": parts})
        return out

    def generate(self, system: str, history: list[HistoryItem], tools: list[ToolSpec]) -> ModelTurn:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(history),
            "tools": [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": self._gen,
        }
        url = f"{BASE_URL}/models/{self.model}:generateContent"
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                # Key in a header, never in the URL (URLs end up in logs)
                resp = client.post(url, json=body, headers={"x-goog-api-key": self._key})
        except httpx.TimeoutException as e:
            raise TransientError(f"Gemini timed out after {self._timeout:g}s") from e
        except httpx.TransportError as e:
            raise TransientError(f"network error reaching Gemini: {type(e).__name__}") from e

        if resp.status_code != 200:
            raise classify_gemini(resp)
        try:
            data = resp.json()
        except ValueError as e:
            raise TransientError("Gemini returned a non-JSON body") from e

        um = data.get("usageMetadata") or {}
        usage = Usage(prompt=um.get("promptTokenCount", 0), output=um.get("candidatesTokenCount", 0),
                      thinking=um.get("thoughtsTokenCount", 0), total=um.get("totalTokenCount", 0))
        candidates = data.get("candidates") or []
        if not candidates:
            block = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise TerminalError(f"Gemini returned no answer (prompt blocked: {block})")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
        calls = [ToolCall(id=p["functionCall"].get("id"), name=p["functionCall"].get("name", ""),
                          args=p["functionCall"].get("args") or {})
                 for p in parts if "functionCall" in p]
        return ModelTurn(text=text, tool_calls=calls, usage=usage, finish_reason=cand.get("finishReason"),
                         raw=parts or [{"text": ""}])