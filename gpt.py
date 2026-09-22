"""
gpt.py -- minimalni klient OpenAI Responses API (POST /v1/responses).

Zamerne bez SDK: staci knihovna requests, kterou agent uz ma. Pouziva se na
dve veci:
  - resers s vyhledavanim na webu (search=True): vyhledavani je VYNUCENE
    (tool_choice="required") a pocet hledani omezeny (max_tool_calls),
  - odpovedi agenta GPT v debate (JSON podle schematu, bez vyhledavani).

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

API_URL = "https://api.openai.com/v1/responses"
RETRY_STATUSES = {429, 500, 502, 503, 504}

# USD za milion tokenu (vstup, vystup) -- jen pro odhad ceny do logu.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5": (1.25, 10.00),
}
SEARCH_PRICE_USD = 10.0 / 1000      # jedno volani vyhledavani


class GPTError(RuntimeError):
    """Volani OpenAI API se nepovedlo."""


@dataclass
class GPTReply:
    text: str
    sources: list[tuple[str, str]] = field(default_factory=list)   # (nazev, odkaz)
    queries: list[str] = field(default_factory=list)                # hledane dotazy
    searches: int = 0                                               # placena hledani
    input_tokens: int = 0
    output_tokens: int = 0


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  searches: int = 0) -> float | None:
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    return ((input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
            + searches * SEARCH_PRICE_USD)


class GPTClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 180.0,
        retries: int = 3,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = max(1, retries)
        self._http = http if http is not None else requests
        self._sleep = sleep
        self._lock = threading.Lock()
        # Soucty za cely beh (klient sdili resers i analytik, i z vice vlaken).
        self.cost_usd = 0.0
        self.cost_known = True
        self.searches = 0

    def generate(
        self,
        messages: list[dict],
        *,
        system: str,
        search: bool = False,
        max_searches: int = 4,
        schema: dict | None = None,
        max_tokens: int = 16000,
        effort: str = "low",
        model: str | None = None,
    ) -> GPTReply:
        """`messages` = [{"role": "user"|"assistant", "content": text}, ...]."""
        model = model or self.model
        body: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": messages,
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": effort},
            "store": False,
        }
        if search:
            body["tools"] = [{"type": "web_search"}]
            body["tool_choice"] = "required"      # hledat MUSI, ne z pameti
            body["max_tool_calls"] = max_searches
        if schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": "verdict",
                                       "schema": schema, "strict": True}}

        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._http.post(API_URL, json=body, headers=headers,
                                       timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    reply = _parse(resp.json())
                    self._account(model, reply)
                    return reply
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUSES:
                    break
                if resp.status_code == 429 and "insufficient_quota" in resp.text:
                    last_error = "doslel kredit na uctu OpenAI (insufficient_quota)"
                    break
            if attempt < self.retries:
                wait = 10.0 * attempt
                log.warning("GPT: %s -- zkusim znovu za %.0f s.", last_error, wait)
                self._sleep(wait)
        raise GPTError(last_error or "neznama chyba")

    def _account(self, model: str, reply: GPTReply) -> None:
        cost = estimate_cost(model, reply.input_tokens, reply.output_tokens, reply.searches)
        with self._lock:
            self.searches += reply.searches
            if cost is None:
                self.cost_known = False
            else:
                self.cost_usd += cost


def _parse(data: dict) -> GPTReply:
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason", "?")
        raise GPTError(f"odpoved nedokoncena ({reason})")
    if status not in (None, "completed"):
        raise GPTError(f"odpoved ve stavu {status}: {data.get('error')}")

    texts: list[str] = []
    sources: list[tuple[str, str]] = []
    queries: list[str] = []
    searches = 0
    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "web_search_call":
            action = item.get("action") or {}
            if action.get("type", "search") == "search":
                searches += 1
                if action.get("query"):
                    queries.append(str(action["query"]))
                queries += [str(q) for q in action.get("queries") or []
                            if str(q) not in queries]
        elif kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "refusal":
                    raise GPTError("model odmitl odpovedet")
                if part.get("type") != "output_text":
                    continue
                texts.append(part.get("text", ""))
                for note in part.get("annotations") or []:
                    if note.get("type") == "url_citation" and note.get("url"):
                        src = (note.get("title") or note["url"], note["url"])
                        if src not in sources:
                            sources.append(src)
    text = "".join(texts).strip()
    if not text:
        raise GPTError("odpoved neobsahuje text")
    usage = data.get("usage") or {}
    return GPTReply(
        text=text, sources=sources, queries=queries, searches=searches,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )
