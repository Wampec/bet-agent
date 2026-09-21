"""
fetcher.py -- komunikace s The Odds API (https://the-odds-api.com/).

Zajistuje:
  * vyber souteci podle sportovniho profilu (Tennis, Basketball, ...),
  * stazeni kurzu pro dnesni den (h2h; volitelne 1X / DNB u fotbalu),
  * stazeni vysledku (scores) pro vyhodnoceni predchozich tipu,
  * sledovani spotreby kreditu API (hlavicky x-requests-used / -remaining).

SPOTREBA KREDITU
----------------
Vypis souteci `/v4/sports/` je zdarma. Kazdy dotaz na `/odds` stoji
1 kredit za kazdy region a kazdy trh. Protoze je `h2h` tzv. featured market,
prijde pro celou soutez v jednom dotazu -- tenis i basketbal tedy stoji
1 kredit za soutez a den. Fotbalove trhy `double_chance` / `draw_no_bet` jsou
naopak doplnkove a musi se dotahovat po jednotlivych zapasech (2 kredity za
zapas), proto je fotbal ve vychozim nastaveni vypnuty.

Strop `MAX_SPORT_KEYS` omezuje pocet dotazovanych souteci a je hlavni
pojistkou proti vycerpani mesicni kvoty.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from config import Config

log = logging.getLogger(__name__)

FEATURED_MARKET = "h2h"
SOCCER_GROUP = "Soccer"
TENNIS_GROUP = "Tennis"
TENNIS_PREFIXES = ("tennis_atp", "tennis_wta")

# Preferovane souteze v ramci skupiny -- pri stropu MAX_SPORT_KEYS se beru prvni.
# Duvod: nejlikvidnejsi souteze maji nejvic bookmakeru, tedy spolehlivejsi
# konsensus a mensi rozptyl odhadu.
PREFERRED_KEYS: tuple[str, ...] = (
    "basketball_nba",
    "basketball_euroleague",
    "basketball_nbl",
    "basketball_ncaab",
    "baseball_mlb",
    "icehockey_nhl",
)


class OddsAPIError(RuntimeError):
    """Chyba pri komunikaci s The Odds API."""


@dataclass
class Quota:
    """Stav kreditu The Odds API podle hlavicek posledni odpovedi."""

    used: int | None = None
    remaining: int | None = None
    last_cost: int | None = None
    requests_made: int = 0

    def update(self, headers: Any) -> None:
        self.requests_made += 1
        for attr, header in (
            ("used", "x-requests-used"),
            ("remaining", "x-requests-remaining"),
            ("last_cost", "x-requests-last"),
        ):
            raw = headers.get(header)
            if raw is None:
                continue
            try:
                setattr(self, attr, int(float(raw)))
            except (TypeError, ValueError):
                continue

    def __str__(self) -> str:
        return (
            f"HTTP dotazu: {self.requests_made}, kredity pouzito: {self.used}, "
            f"zbyva: {self.remaining}"
        )


@dataclass
class Event:
    """Jeden zapas i s kurzy vsech bookmakeru."""

    event_id: str
    sport_key: str
    sport_title: str
    sport_group: str
    commence_time: datetime
    home_team: str
    away_team: str
    # {market_key: [{"bookmaker": str, "outcomes": {name: price}}, ...]}
    markets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def is_soccer(self) -> bool:
        return self.sport_group == SOCCER_GROUP or self.sport_key.startswith("soccer")

    @property
    def is_tennis(self) -> bool:
        return self.sport_key.startswith(TENNIS_PREFIXES)

    @property
    def is_grand_slam(self) -> bool:
        return any(
            slam in self.sport_key
            for slam in ("aus_open", "french_open", "wimbledon", "us_open")
        )

    @property
    def label(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


# --------------------------------------------------------------------------- #
# HTTP klient
# --------------------------------------------------------------------------- #
class OddsAPIClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.quota = Quota()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "bet-agent/2.0"})
        self._sports_cache: list[dict[str, Any]] | None = None

    # -- nizkourovnovy dotaz ------------------------------------------------ #
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.cfg.odds_api_base}{path}"
        query = dict(params or {})
        query["apiKey"] = self.cfg.odds_api_key

        last_error: Exception | None = None
        for attempt in range(1, self.cfg.http_retries + 1):
            try:
                resp = self._session.get(
                    url, params=query, timeout=self.cfg.http_timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Sitova chyba (%s/%s) %s: %s",
                            attempt, self.cfg.http_retries, path, exc)
                time.sleep(min(2 ** attempt, 8))
                continue

            self.quota.update(resp.headers)

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                raise OddsAPIError("The Odds API: neplatny ODDS_API_KEY (401).")
            if resp.status_code == 422:
                # Napr. soutez bez pozadovaneho trhu -- neni to fatalni chyba.
                log.debug("422 pro %s (%s): %s", path, query.get("markets"), resp.text)
                return None
            if resp.status_code == 429:
                raise OddsAPIError("The Odds API: vycerpana kvota / rate limit (429).")
            if 500 <= resp.status_code < 600:
                last_error = OddsAPIError(f"HTTP {resp.status_code}")
                log.warning("Chyba serveru (%s/%s) %s: HTTP %s",
                            attempt, self.cfg.http_retries, path, resp.status_code)
                time.sleep(min(2 ** attempt, 8))
                continue
            raise OddsAPIError(
                f"The Odds API {path}: HTTP {resp.status_code} -- {resp.text[:300]}"
            )

        raise OddsAPIError(f"The Odds API {path}: dotaz selhal ({last_error})")

    # -- sporty ------------------------------------------------------------- #
    def list_sports(self) -> list[dict[str, Any]]:
        """Vypis aktivnich souteci (tenhle dotaz je zdarma, nestoji kredit)."""
        if self._sports_cache is None:
            data = self._get("/sports/") or []
            self._sports_cache = [s for s in data if s.get("active")]
            log.debug("Aktivnich souteci celkem: %s", len(self._sports_cache))
        return self._sports_cache

    def select_sport_keys(self) -> list[str]:
        """
        Vybere souteze k dotazu podle SPORT_GROUPS, serazene podle priority
        a orezane na MAX_SPORT_KEYS. Vysledek primo urcuje spotrebu kreditu.
        """
        cfg = self.cfg
        wanted = {g.lower() for g in cfg.sport_groups}
        blacklist = set(cfg.sport_key_blacklist)

        if cfg.sport_key_whitelist:
            available = {s["key"] for s in self.list_sports()}
            keys = [
                k for k in cfg.sport_key_whitelist
                if k in available and k not in blacklist
            ]
            for missing in (k for k in cfg.sport_key_whitelist if k not in available):
                log.info("Soutez '%s' neni prave aktivni -- preskakuji.", missing)
            return keys[: cfg.max_sport_keys]

        candidates: list[tuple[int, str]] = []
        tennis_seen: list[tuple[bool, str]] = []

        for sport in self.list_sports():
            key = sport.get("key", "")
            group = (sport.get("group") or "").lower()
            if group not in wanted or sport.get("has_outrights") or key in blacklist:
                continue
            if group == TENNIS_GROUP.lower():
                if not key.startswith(TENNIS_PREFIXES):
                    continue
                tennis_seen.append((self._is_top_tier_tennis(key), key))
                continue
            priority = PREFERRED_KEYS.index(key) if key in PREFERRED_KEYS else 99
            candidates.append((priority, key))

        # Tenis: nejdriv turnaje nejvyssi urovne, pak zbytek (pokud je povolen).
        top = [k for is_top, k in tennis_seen if is_top]
        rest = [k for is_top, k in tennis_seen if not is_top]
        if self.cfg.tennis_top_tier_only:
            if not top and rest:
                log.info(
                    "Zadny turnaj nejvyssi urovne nebezi; TENNIS_TOP_TIER_ONLY "
                    "je zapnute, takze %s turnaju nizsi urovne preskakuji.", len(rest)
                )
            rest = []
        tennis_keys = top + rest

        keys = tennis_keys + [k for _, k in sorted(candidates)]
        if len(keys) > cfg.max_sport_keys:
            log.info(
                "Nalezeno %s souteci, strop MAX_SPORT_KEYS=%s -- dotazu se prvnich %s.",
                len(keys), cfg.max_sport_keys, cfg.max_sport_keys,
            )
        return keys[: cfg.max_sport_keys]

    def soccer_sport_keys(self) -> list[str]:
        available = {
            s["key"]: s
            for s in self.list_sports()
            if s.get("group") == SOCCER_GROUP and not s.get("has_outrights")
        }
        whitelist = self.cfg.soccer_league_whitelist
        if not whitelist:
            return sorted(available)
        keys = [k for k in whitelist if k in available]
        for missing in (k for k in whitelist if k not in available):
            log.info("Liga '%s' neni prave ted aktivni -- preskakuji.", missing)
        return keys

    def _is_top_tier_tennis(self, sport_key: str) -> bool:
        haystack = sport_key.lower()
        return any(kw.lower() in haystack for kw in self.cfg.tennis_tier_keywords)

    # -- kurzy -------------------------------------------------------------- #
    def fetch_odds(
        self,
        sport_key: str,
        markets: Iterable[str],
        time_from: datetime,
        time_to: datetime,
    ) -> list[dict[str, Any]]:
        params = {
            "regions": ",".join(self.cfg.regions),
            "markets": ",".join(markets),
            "oddsFormat": self.cfg.odds_format,
            "dateFormat": "iso",
            "commenceTimeFrom": _iso_z(time_from),
            "commenceTimeTo": _iso_z(time_to),
        }
        data = self._get(f"/sports/{sport_key}/odds/", params)
        return data or []

    def fetch_event_markets(
        self, sport_key: str, event_id: str, markets: Iterable[str]
    ) -> dict[str, Any] | None:
        """Doplnkove trhy (double_chance, draw_no_bet) pro jeden konkretni zapas."""
        params = {
            "regions": ",".join(self.cfg.regions),
            "markets": ",".join(markets),
            "oddsFormat": self.cfg.odds_format,
            "dateFormat": "iso",
        }
        return self._get(f"/sports/{sport_key}/events/{event_id}/odds/", params)

    def fetch_scores(self, sport_key: str, days_from: int = 3) -> list[dict[str, Any]]:
        """Vysledky ukoncenych zapasu -- pro vyhodnoceni predchoziho tipu."""
        params = {"daysFrom": max(1, min(days_from, 3)), "dateFormat": "iso"}
        data = self._get(f"/sports/{sport_key}/scores/", params)
        return data or []


# --------------------------------------------------------------------------- #
# Casove okno "dnesniho dne"
# --------------------------------------------------------------------------- #
def day_window(cfg: Config, now: datetime | None = None) -> tuple[datetime, datetime]:
    """
    Vrati (od, do) v UTC:
      od = ted + MIN_MINUTES_BEFORE_START (vychozi 0 = od tohoto okamziku),
      do = drive z [konec dnesniho dne v lokalni zone, ted + MAX_HOURS_AHEAD].
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now + timedelta(minutes=cfg.min_minutes_before_start)
    end = now + timedelta(hours=cfg.max_hours_ahead)

    if cfg.same_day_only:
        local_now = now.astimezone(cfg.tz)
        local_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
        end = min(end, local_end.astimezone(timezone.utc))

    if end <= start:
        end = start + timedelta(minutes=1)
    return start, end


def _iso_z(value: datetime) -> str:
    """The Odds API vyzaduje ISO8601 v UTC bez zlomku sekund (2026-09-09T18:00:00Z)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Prevod odpovedi API na objekty Event
# --------------------------------------------------------------------------- #
def _parse_event(
    payload: dict[str, Any], sport_meta: dict[str, dict[str, Any]]
) -> Event | None:
    try:
        sport_key = payload["sport_key"]
        event = Event(
            event_id=payload["id"],
            sport_key=sport_key,
            sport_title=payload.get("sport_title") or sport_key,
            sport_group=sport_meta.get(sport_key, {}).get("group", ""),
            commence_time=_parse_time(payload["commence_time"]),
            home_team=payload.get("home_team") or "",
            away_team=payload.get("away_team") or "",
        )
    except (KeyError, ValueError) as exc:
        log.warning("Preskakuji nevalidni zapas z API: %s", exc)
        return None

    merge_markets(event, payload)
    return event


def merge_markets(event: Event, payload: dict[str, Any]) -> None:
    """Doplni do udalosti kurzy vsech bookmakeru z odpovedi API."""
    for bookmaker in payload.get("bookmakers", []):
        book_name = bookmaker.get("title") or bookmaker.get("key") or "?"
        for market in bookmaker.get("markets", []):
            key = market.get("key")
            outcomes = {
                o["name"]: float(o["price"])
                for o in market.get("outcomes", [])
                if o.get("name") and o.get("price")
            }
            if not key or len(outcomes) < 2:
                continue
            entry = {"bookmaker": book_name, "outcomes": outcomes}
            bucket = event.markets.setdefault(key, [])
            # Stejny bookmaker pro stejny trh nechceme zapocitat dvakrat.
            if not any(e["bookmaker"] == book_name for e in bucket):
                bucket.append(entry)


def fetch_daily_events(client: OddsAPIClient, now: datetime | None = None) -> list[Event]:
    """
    Stahne dnesni zapasy vybranych sportu se zakladnim trhem h2h.
    Fotbalove odvozene trhy se dotahuji az v enrich_soccer_markets().
    """
    cfg = client.cfg
    time_from, time_to = day_window(cfg, now)
    log.info(
        "Casove okno: %s -- %s (%s)",
        time_from.astimezone(cfg.tz).strftime("%d.%m. %H:%M"),
        time_to.astimezone(cfg.tz).strftime("%d.%m. %H:%M"),
        cfg.timezone,
    )

    sport_meta = {s["key"]: s for s in client.list_sports()}
    sport_keys = client.select_sport_keys()
    if sport_keys:
        log.info("Souteze k dotazu (%s): %s", len(sport_keys), ", ".join(sport_keys))
    else:
        log.warning(
            "Zadna soutez ze skupin %s prave nebezi.", ", ".join(cfg.sport_groups)
        )

    if cfg.enable_soccer:
        soccer_keys = client.soccer_sport_keys()
        log.info("Fotbalovych lig k dotazu: %s", len(soccer_keys))
        sport_keys = sport_keys + soccer_keys

    events: list[Event] = []
    for sport_key in sport_keys:
        try:
            payloads = client.fetch_odds(
                sport_key, [FEATURED_MARKET], time_from, time_to
            )
        except OddsAPIError as exc:
            log.error("Kurzy pro '%s' se nepodarilo stahnout: %s", sport_key, exc)
            continue

        for payload in payloads:
            event = _parse_event(payload, sport_meta)
            if event is None:
                continue
            # Pojistka: API obcas vraci i zapasy tesne mimo pozadovane okno.
            if not (time_from <= event.commence_time <= time_to):
                continue
            events.append(event)

    log.info("Nacteno %s zapasu v dnesnim okne. %s", len(events), client.quota)
    return events


def enrich_soccer_markets(client: OddsAPIClient, events: list[Event]) -> None:
    """
    Pro predane fotbalove zapasy dotahne trhy double_chance / draw_no_bet.
    Volat az na kratkem seznamu kandidatu -- kazdy zapas stoji 2 kredity.
    """
    cfg = client.cfg
    markets = [m for m in cfg.soccer_markets if m]
    if not markets or cfg.max_event_market_lookups <= 0:
        return

    for event in events[: cfg.max_event_market_lookups]:
        try:
            payload = client.fetch_event_markets(event.sport_key, event.event_id, markets)
        except OddsAPIError as exc:
            log.error("Doplnkove trhy pro %s selhaly: %s", event.label, exc)
            continue
        if not payload:
            log.info("Trhy %s nejsou dostupne pro %s.", markets, event.label)
            continue
        merge_markets(event, payload)
