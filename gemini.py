"""
gemini.py -- minimalni klient Gemini API (REST models.generateContent).

Zamerne bez SDK od Googlu: staci knihovna requests, kterou agent uz ma, a
REST rozhrani se meni mene nez SDK. Pouziva se na dve veci:
  - resers s vyhledavanim Google (search=True) -- v bezplatne urovni ho maji
    jen gemini-2.5-flash a 2.5-flash-lite (500 dotazu denne, spolecne),
  - odpovedi agenta Gemini v debate (JSON podle schematu, bez vyhledavani).

Klic se posila v hlavicce, ne v URL -- nedostane se tak do logu s chybou.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    """Volani Gemini API se nepovedlo."""


@dataclass
class GeminiReply:
    text: str
    sources: list[tuple[str, str]] = field(default_factory=list)   # (nazev, odkaz)
    queries: list[str] = field(default_factory=list)                # hledane dotazy
    input_tokens: int = 0
    output_tokens: int = 0


def strip_additional_properties(schema: Any) -> Any:
    """Gemini nechce 'additionalProperties' (Anthropic ho naopak vyzaduje)."""
    if isinstance(schema, dict):
        return {k: strip_additional_properties(v) for k, v in schema.items()
                if k != "additionalProperties"}
    if isinstance(schema, list):
        return [strip_additional_properties(v) for v in schema]
    return schema


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 180.0,
        retries: int = 3,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = max(1, retries)
        # requests.post je bezpecne volat z vice vlaken (Session nemusi byt).
        self._http = http if http is not None else requests
        self._sleep = sleep
        # Bezplatna uroven ma limit dotazu za minutu -- dotazy (i z vice vlaken)
        # se proto spousti nejdriv min_interval sekund po sobe.
        self.min_interval = max(0.0, min_interval)
        self._clock = clock
        self._lock = threading.Lock()
        self._next_start = 0.0

    def _wait_turn(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next_start)
            self._next_start = start + self.min_interval
        if start > now:
            self._sleep(start - now)

    def generate(
        self,
        contents: list[dict],
        *,
        system: str,
        search: bool = False,
        schema: dict | None = None,
        max_tokens: int = 8192,
    ) -> GeminiReply:
        body: dict[str, Any] = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if search:
            body["tools"] = [{"googleSearch": {}}]
        if schema is not None:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseJsonSchema"] = strip_additional_properties(schema)

        url = API_URL.format(model=self.model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            try:
                resp = self._http.post(url, json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return _parse(resp.json())
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUSES:
                    break
            if attempt < self.retries:
                wait = 15.0 * attempt
                log.warning("Gemini: %s -- zkusim znovu za %.0f s.", last_error, wait)
                self._sleep(wait)
        raise GeminiError(last_error or "neznama chyba")


def _parse(data: dict) -> GeminiReply:
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise GeminiError(f"dotaz zablokovan ({block})")
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("odpoved bez kandidatu")
    cand = candidates[0]
    finish = cand.get("finishReason", "STOP")
    if finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        raise GeminiError(f"odpoved skoncila predcasne ({finish})")

    parts = (cand.get("content") or {}).get("parts") or []
    # Casti s "thought" jsou premysleni modelu, ne odpoved.
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError("odpoved neobsahuje text")

    grounding = cand.get("groundingMetadata") or {}
    sources: list[tuple[str, str]] = []
    for chunk in grounding.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        if web.get("uri"):
            item = (web.get("title") or web["uri"], web["uri"])
            if item not in sources:
                sources.append(item)

    usage = data.get("usageMetadata") or {}
    return GeminiReply(
        text=text,
        sources=sources,
        queries=[str(q) for q in grounding.get("webSearchQueries") or []],
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0)
        + int(usage.get("thoughtsTokenCount") or 0),
    )
