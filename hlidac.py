"""
hlidac.py -- hlidac zivych kurzu. Bezi na tvem pocitaci.

Pravidlo
--------
Fotbalovy favorit s kurzem do HLIDAC_MAX_ODDS (1,5) dostane gol do
HLIDAC_GOAL_MINUTE (15.) a prohrava presne 0:1. Historicka data (16 evropskych
lig, 2000-2023, ~900 takovych zapasu) rikaji, ze favorit presto vyhraje
v ~54 % (ferovy kurz 1,85) a neprohraje v ~75 % (ferovy kurz 1,33) -- vic, nez
by odpovidalo prostemu modelu. Pokud kancelar v tu chvili nabizi kurz nad
ferovym + rezerva, ma sazka kladnou hodnotu.

Hlidac NIC NESAZI. Jen:
  - rano posle seznam dnesnich favoritu ve sledovanych ligach,
  - behem zapasu hlida skore a pri spusteni pravidla stahne zivy kurz,
  - posle na Telegram zpravu (VALUE / bez value) a zapise pripad do CSV,
  - po zapase dopise vysledek.

Zdroje dat (bez registrace):
  - ESPN (neoficialni verejne API, zdarma, bez klice): program, predzapasovy
    kurz (DraftKings), zive skore a minuta golu, vysledek.
  - The Odds API (klic ODDS_API_KEY, uz ho mame): zivy kurz evropskych
    kancelari, jen pri spusteni pravidla -- 1 kredit za dotaz.

Ferove kurzy jsou odhad z historickych dat bez kurzu (sila favorita z Elo),
takze je ber jako hypotezu, kterou zapsane pripady potvrdi nebo vyvrati.

Prikazy:
    python hlidac.py plan [RRRR-MM-DD]  favorite dne (zdarma, nic neposila)
    python hlidac.py diag               kontrola lig, kreditu a Telegramu
    python hlidac.py run                hlidani (nechat bezet, Ctrl+C ukonci)
    python hlidac.py report             vyhodnoceni zapsanych pripadu

Nastaveni je v settings.env (HLIDAC_*), Telegram stejne jako u denniho agenta.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import re
import socket
import statistics
import sys
import time
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import requests

from config import BASE_DIR, Config, ConfigError, enable_utf8_stdout, load_config
from notifier import TelegramError, TelegramNotifier

log = logging.getLogger("hlidac")

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{key}/odds/"
STATE_FILE = BASE_DIR / "hlidac_stav.json"
CASES_FILE = BASE_DIR / "hlidac_pripady.csv"
LOG_FILE = BASE_DIR / "hlidac.log"
LOCK_PORT = 47613          # jedna instance: obsazeny port = hlidac uz bezi

# Liga ESPN -> soutez The Odds API (pro zivy kurz). Bez mapovani se pripad
# zapise jen s predzapasovym kurzem.
ESPN_TO_ODDS: dict[str, str] = {
    "eng.1": "soccer_epl",
    "eng.2": "soccer_efl_champ",
    "esp.1": "soccer_spain_la_liga",
    "ger.1": "soccer_germany_bundesliga",
    "ita.1": "soccer_italy_serie_a",
    "fra.1": "soccer_france_ligue_one",
    "ned.1": "soccer_netherlands_eredivisie",
    "por.1": "soccer_portugal_primeira_liga",
    "bel.1": "soccer_belgium_first_div",
    "tur.1": "soccer_turkey_super_league",
    "sco.1": "soccer_spl",
    "aut.1": "soccer_austria_bundesliga",
    "sui.1": "soccer_switzerland_superleague",
    "gre.1": "soccer_greece_super_league",
    "nor.1": "soccer_norway_eliteserien",
    "den.1": "soccer_denmark_superliga",
    "uefa.champions": "soccer_uefa_champs_league",
    "uefa.europa": "soccer_uefa_europa_league",
    "uefa.europa.conf": "soccer_uefa_europa_conference_league",
}

# Ferove kurzy z historickych dat: (max predzapasovy kurz favorita,
# ferovy kurz na vyhru, ferovy kurz na dvojtip "favorit neprohraje").
#   do 1,40:   po 0:1 do 15. min vyhraje 61,9 %  -> 1,61;  neprohraje 81 % -> 1,23
#   1,40-1,50: vyhraje 48,7 %                    -> 2,05;  neprohraje 71 % -> 1,41
FAIR_ODDS: tuple[tuple[float, float, float], ...] = ((1.40, 1.61, 1.23), (1.50, 2.05, 1.41))

CSV_COLUMNS = [
    "datum", "liga", "domaci", "hoste", "zapas_id", "favorit", "kurz_pred",
    "minuta_golu", "minuta_zaznamu", "live_vyhra", "live_vyhra_max", "live_remiza",
    "live_soupere", "live_dvojtip", "kancelari", "ferovy_vyhra", "ferovy_dvojtip",
    "value_vyhra", "value_dvojtip", "konec", "favorit_vyhral", "favorit_neprohral",
]
DEAD_STATUSES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED",
                 "STATUS_FORFEIT", "STATUS_SUSPENDED"}


class HlidacError(RuntimeError):
    """Chyba pri stahovani dat."""


# --------------------------------------------------------------------------- #
# Stav a zapis
# --------------------------------------------------------------------------- #
def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def append_case(row: dict[str, Any], path: Path = CASES_FILE) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, delimiter=";")
        if new:
            writer.writeheader()
        writer.writerow(row)


def read_cases(path: Path = CASES_FILE) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def write_cases(rows: list[dict[str, str]], path: Path = CASES_FILE) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# ESPN: program, skore, minuta golu, predzapasovy kurz
# --------------------------------------------------------------------------- #
def american_to_decimal(raw: Any) -> float | None:
    """'+215' -> 3.15, '-250' -> 1.40; jine hodnoty None."""
    try:
        value = float(str(raw).replace("+", "").strip())
    except (TypeError, ValueError):
        return None
    if value >= 100:
        return round(1 + value / 100, 3)
    if value <= -100:
        return round(1 + 100 / abs(value), 3)
    return None


def _moneyline(odds: dict[str, Any], side: str) -> float | None:
    """Kurz z ESPN odds (DraftKings): zaviraci, jinak otviraci."""
    line = (odds.get("moneyline") or {}).get(side) or {}
    for phase in ("close", "open"):
        value = american_to_decimal((line.get(phase) or {}).get("odds"))
        if value:
            return value
    if side == "draw":
        return american_to_decimal((odds.get("drawOdds") or {}).get("moneyLine"))
    return None


def parse_event(event: dict[str, Any], league: str) -> dict[str, Any] | None:
    """Zjednoduseny zapas z ESPN scoreboardu."""
    try:
        comp = event["competitions"][0]
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        home, away = sides["home"], sides["away"]
    except (KeyError, IndexError, TypeError):
        return None
    status = event.get("status") or {}
    stype = status.get("type") or {}
    odds_list = comp.get("odds") or []
    odds = odds_list[0] if odds_list and odds_list[0] else {}
    goals = []
    for detail in comp.get("details") or []:
        if not detail.get("scoringPlay"):
            continue
        seconds = (detail.get("clock") or {}).get("value")
        minute = int(seconds // 60) + 1 if isinstance(seconds, (int, float)) else None
        goals.append({"team_id": str((detail.get("team") or {}).get("id", "")), "minute": minute})
    clock = status.get("clock")
    return {
        "id": str(event.get("id")),
        "league": league,
        "kickoff": event.get("date"),
        "state": stype.get("state"),            # pre | in | post
        "status": stype.get("name", ""),
        "minute": int(clock // 60) + 1 if isinstance(clock, (int, float)) and stype.get("state") == "in" else 0,
        "home": home["team"].get("displayName", ""), "away": away["team"].get("displayName", ""),
        "home_id": str(home["team"].get("id", "")), "away_id": str(away["team"].get("id", "")),
        "home_score": _int(home.get("score")), "away_score": _int(away.get("score")),
        "odds_home": _moneyline(odds, "home"), "odds_draw": _moneyline(odds, "draw"),
        "odds_away": _moneyline(odds, "away"),
        "goals": goals,
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _kickoff(c: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(c["kickoff"].replace("Z", "+00:00"))


class Espn:
    def __init__(self, cfg: Config, http: Any = None) -> None:
        self.cfg = cfg
        self._http = http if http is not None else requests.Session()

    def scoreboard(self, league: str, day: str) -> list[dict[str, Any]]:
        """day = 'RRRRMMDD'. ESPN pocita den v americkem case, proto se bere i dalsi den."""
        try:
            resp = self._http.get(ESPN_URL.format(league=league), params={"dates": day},
                                  timeout=self.cfg.http_timeout)
        except requests.RequestException as exc:
            raise HlidacError(f"ESPN {league}: {exc}") from exc
        if resp.status_code != 200:
            raise HlidacError(f"ESPN {league}: HTTP {resp.status_code}")
        events = (resp.json() or {}).get("events") or []
        return [e for e in (parse_event(ev, league) for ev in events) if e]


# --------------------------------------------------------------------------- #
# The Odds API: zivy kurz (1 kredit za dotaz)
# --------------------------------------------------------------------------- #
_NOISE = {"fc", "afc", "cf", "sc", "ac", "as", "ssc", "sv", "fk", "bk", "if", "cd", "ud",
          "rc", "rcd", "club", "de", "the", "calcio", "1", "04", "05", "1899", "1846", "1909"}


def norm_team(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in _NOISE]
    return " ".join(words)


def team_similarity(a: str, b: str) -> float:
    na, nb = norm_team(a), norm_team(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def match_odds_event(events: list[dict[str, Any]], cand: dict[str, Any]) -> dict[str, Any] | None:
    """Najde v nabidce The Odds API stejny zapas (podle casu a nazvu tymu)."""
    kickoff = _kickoff(cand)
    best, best_score = None, 0.0
    for ev in events:
        try:
            start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if abs((start - kickoff).total_seconds()) > 3 * 3600:
            continue
        score = (team_similarity(ev.get("home_team", ""), cand["home"])
                 + team_similarity(ev.get("away_team", ""), cand["away"])) / 2
        if score > best_score:
            best, best_score = ev, score
    return best if best_score >= 0.6 else None


def live_prices(event: dict[str, Any], fav_name: str, opp_name: str) -> dict[str, Any]:
    """Z h2h nabidek kancelari: median a nejlepsi kurz favorita, remiza, souper, odhad dvojtipu."""
    fav_prices, draw_prices, opp_prices = [], [], []
    for bm in event.get("bookmakers") or []:
        for market in bm.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            outcomes = {o.get("name"): o.get("price") for o in market.get("outcomes") or []}
            fav = next((p for n, p in outcomes.items() if n and team_similarity(n, fav_name) >= 0.8), None)
            opp = next((p for n, p in outcomes.items() if n and team_similarity(n, opp_name) >= 0.8), None)
            draw = outcomes.get("Draw")
            if fav and draw and opp:
                fav_prices.append(float(fav))
                draw_prices.append(float(draw))
                opp_prices.append(float(opp))
    if not fav_prices:
        return {"win": None, "win_max": None, "draw": None, "opp": None, "dc": None, "books": 0}
    win = statistics.median(fav_prices)
    draw = statistics.median(draw_prices)
    return {
        "win": round(win, 2), "win_max": round(max(fav_prices), 2),
        "draw": round(draw, 2), "opp": round(statistics.median(opp_prices), 2),
        # Dvojtip se v nabidce h2h nestahuje (stal by kredit navic): odhad z vyhry a remizy.
        "dc": round(1 / (1 / win + 1 / draw), 2),
        "books": len(fav_prices),
    }


class OddsApi:
    def __init__(self, cfg: Config, state: dict[str, Any], http: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self._http = http if http is not None else requests.Session()

    def _budget_ok(self) -> bool:
        today = datetime.now(self.cfg.tz).strftime("%Y-%m-%d")
        if self.state.get("odds_den") != today:
            self.state["odds_den"], self.state["odds_pocet"] = today, 0
        return self.state["odds_pocet"] < self.cfg.hlidac_max_odds_calls

    def live_events(self, sport_key: str) -> list[dict[str, Any]]:
        if not self.cfg.odds_api_key:
            raise HlidacError("chybi ODDS_API_KEY")
        if not self._budget_ok():
            raise HlidacError("denni strop dotazu na zive kurzy (HLIDAC_MAX_ODDS_CALLS)")
        try:
            resp = self._http.get(ODDS_URL.format(key=sport_key), timeout=self.cfg.http_timeout,
                                  params={"apiKey": self.cfg.odds_api_key, "regions": "eu",
                                          "markets": "h2h", "oddsFormat": "decimal"})
        except requests.RequestException as exc:
            raise HlidacError(f"The Odds API: {type(exc).__name__}") from exc
        self.state["odds_pocet"] = self.state.get("odds_pocet", 0) + 1
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            self.state["odds_zbyva"] = remaining
        if resp.status_code != 200:
            raise HlidacError(f"The Odds API: HTTP {resp.status_code} {resp.text[:150]}")
        now = datetime.now(timezone.utc)
        out = []
        for ev in resp.json() or []:
            try:
                start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if start <= now:                 # jen rozehrane zapasy
                out.append(ev)
        return out


# --------------------------------------------------------------------------- #
# Logika
# --------------------------------------------------------------------------- #
def fair_for(pre_odds: float) -> tuple[float, float] | tuple[None, None]:
    for max_pre, fair_win, fair_dc in FAIR_ODDS:
        if pre_odds <= max_pre:
            return fair_win, fair_dc
    return None, None


def favourite(match: dict[str, Any], max_odds: float) -> tuple[str, float] | None:
    """('home'|'away', kurz), kdyz ma zapas favorita do max_odds."""
    home, away = match.get("odds_home"), match.get("odds_away")
    if not home or not away:
        return None
    fav, pre = ("home", home) if home <= away else ("away", away)
    return (fav, round(pre, 2)) if pre <= max_odds else None


def check_trigger(match: dict[str, Any], cand: dict[str, Any], max_goal_minute: int = 15,
                  max_elapsed: int = 18) -> tuple[str, Any]:
    """
    ("trigger", minuta_golu) | ("pryc", duvod) | ("cekej", None)
    Pravidlo: favorit prohrava presne 0:1 a gol soupere padl do max_goal_minute.
    """
    if match["status"] in DEAD_STATUSES:
        return "pryc", "zruseno/odlozeno"
    if match["state"] == "pre":
        return "cekej", None
    if match["state"] == "post":
        return "pryc", "konec zapasu"
    fav_goals, opp_goals = ((match["home_score"], match["away_score"]) if cand["fav"] == "home"
                            else (match["away_score"], match["home_score"]))
    elapsed = match["minute"]
    if fav_goals == 0 and opp_goals == 1:
        # Jediny gol zapasu -- jeho minuta (u vlastence ESPN pise tym ruzne,
        # proto se nebere ohled na to, komu je gol pripsany).
        minutes = [g["minute"] for g in match["goals"] if g["minute"] is not None]
        minute = minutes[0] if minutes else elapsed
        if minute <= max_goal_minute:
            return "trigger", minute
        return "pryc", f"gol az v {minute}. min"
    if elapsed > max_elapsed or fav_goals > 0 or opp_goals > 1:
        return "pryc", f"{fav_goals}:{opp_goals} v {elapsed}. min"
    return "cekej", None


def _send(notifier: TelegramNotifier | None, text: str) -> None:
    if notifier is None:
        log.info("(Telegram nenastaven) %s", text.replace("\n", " | "))
        return
    try:
        notifier.send(text, parse_mode="")
    except TelegramError as exc:
        log.error("Telegram: %s", exc)


def fetch_day(espn: Espn, cfg: Config, day: datetime) -> list[dict[str, Any]]:
    """Zapasy sledovanych lig s vykopem v danem dni (prazsky cas)."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    out, seen = [], set()
    for league in cfg.hlidac_leagues:
        for d in (start, end):                  # ESPN dny jsou v americkem case
            try:
                matches = espn.scoreboard(league, d.strftime("%Y%m%d"))
            except HlidacError as exc:
                log.warning("%s", exc)
                continue
            for m in matches:
                if m["id"] in seen or not m["kickoff"]:
                    continue
                kickoff = _kickoff(m).astimezone(cfg.tz)
                if start <= kickoff < end:
                    seen.add(m["id"])
                    out.append(m)
    return out


def plan_day(espn: Espn, cfg: Config, day: datetime) -> dict[str, dict[str, Any]]:
    """Kandidati dne: favorite do HLIDAC_MAX_ODDS, kteri jeste nehraji."""
    candidates: dict[str, dict[str, Any]] = {}
    for m in fetch_day(espn, cfg, day):
        if m["state"] != "pre":
            continue
        fav = favourite(m, cfg.hlidac_max_odds)
        if fav is None:
            continue
        side, pre = fav
        candidates[m["id"]] = {
            "league": m["league"], "kickoff": m["kickoff"], "home": m["home"], "away": m["away"],
            "home_id": m["home_id"], "away_id": m["away_id"],
            "fav": side, "pre": pre, "stav": "ceka", "pokusy_kurz": 0,
        }
    return candidates


def format_plan(candidates: dict[str, dict[str, Any]], cfg: Config) -> str:
    if not candidates:
        return f"Hlidac: dnes zadny favorit do {cfg.hlidac_max_odds} ve sledovanych ligach."
    lines = []
    for c in sorted(candidates.values(), key=lambda c: c["kickoff"]):
        when = _kickoff(c).astimezone(cfg.tz).strftime("%H:%M")
        lines.append(f"{when} {c['home']} - {c['away']} (favorit {c[c['fav']]} {c['pre']:.2f}, {c['league']})")
    return (f"Hlidac: dnes {len(candidates)} favoritu do {cfg.hlidac_max_odds}\n" + "\n".join(lines[:40]))


def record_case(odds_api: OddsApi | None, cfg: Config, state: dict[str, Any], mid: str,
                cand: dict[str, Any], minute: int, elapsed: int,
                notifier: TelegramNotifier | None) -> bool:
    """Stahne zivy kurz, zapise pripad, posle zpravu. False = kurz jeste neni, zkusit znovu."""
    fav_name = cand[cand["fav"]]
    opp_name = cand["away"] if cand["fav"] == "home" else cand["home"]
    prices = {"win": None, "win_max": None, "draw": None, "opp": None, "dc": None, "books": 0}
    sport_key = ESPN_TO_ODDS.get(cand["league"])
    if odds_api is not None and sport_key:
        try:
            event = match_odds_event(odds_api.live_events(sport_key), cand)
            if event:
                prices = live_prices(event, fav_name, opp_name)
            else:
                log.warning("Zapas %s - %s v nabidce The Odds API nenalezen.", cand["home"], cand["away"])
        except HlidacError as exc:
            log.warning("Zivy kurz: %s", exc)
    if not prices["win"] and cand["pokusy_kurz"] < 2 and sport_key and odds_api is not None:
        cand["pokusy_kurz"] += 1          # kurzy byvaji po golu chvili zavrene
        return False

    fair_win, fair_dc = fair_for(cand["pre"])
    margin = cfg.hlidac_value_margin
    value_win = bool(prices["win"] and fair_win and prices["win"] >= fair_win * margin)
    value_dc = bool(prices["dc"] and fair_dc and prices["dc"] >= fair_dc * margin)
    append_case({
        "datum": datetime.now(cfg.tz).strftime("%Y-%m-%d"), "liga": cand["league"],
        "domaci": cand["home"], "hoste": cand["away"], "zapas_id": mid,
        "favorit": fav_name, "kurz_pred": cand["pre"], "minuta_golu": minute, "minuta_zaznamu": elapsed,
        "live_vyhra": prices["win"], "live_vyhra_max": prices["win_max"], "live_remiza": prices["draw"],
        "live_soupere": prices["opp"], "live_dvojtip": prices["dc"], "kancelari": prices["books"],
        "ferovy_vyhra": fair_win, "ferovy_dvojtip": fair_dc,
        "value_vyhra": int(value_win), "value_dvojtip": int(value_dc),
        "konec": "", "favorit_vyhral": "", "favorit_neprohral": "",
    })
    state.setdefault("k_dohrani", {})[mid] = {"fav": cand["fav"], "league": cand["league"],
                                              "kickoff": cand["kickoff"]}

    def fmt(v: Any) -> str:
        return f"{v:.2f}" if v else "-"

    head = "VALUE" if (value_win or value_dc) else "bez value"
    if prices["win"]:
        live = (f"Zive (median {prices['books']} kancelari): vyhra {fmt(prices['win'])} "
                f"(nejlepsi {fmt(prices['win_max'])}, ferovy {fair_win}, hranice {fair_win * margin:.2f})"
                f" | dvojtip ~{fmt(prices['dc'])} (ferovy {fair_dc}, hranice {fair_dc * margin:.2f})")
    else:
        live = (f"Zivy kurz se nepodarilo zjistit. Hranice: vyhra {fair_win * margin:.2f}, "
                f"dvojtip {fair_dc * margin:.2f}")
    _send(notifier,
          f"{head}: {cand['home']} - {cand['away']} 0:1 proti favoritovi ({minute}. min)\n"
          f"Favorit {fav_name}, kurz pred zapasem {cand['pre']:.2f}\n{live}\n"
          f"Zkontroluj kurz u Tipsportu/Fortuny. Nic se nesazi automaticky.")
    log.info("ZAZNAM %s - %s, zive %s", cand["home"], cand["away"], prices)
    return True


def finish_results(espn: Espn, cfg: Config, state: dict[str, Any]) -> int:
    """Doplni konecny vysledek u zapsanych pripadu (ESPN, zdarma)."""
    pending: dict[str, dict[str, Any]] = state.get("k_dohrani", {})
    if not pending:
        return 0
    done: dict[str, tuple[str, int, int]] = {}
    for mid, info in list(pending.items()):
        day = _kickoff(info).astimezone(cfg.tz)
        try:
            matches = fetch_day(espn, replace(cfg, hlidac_leagues=[info["league"]]), day)
        except HlidacError as exc:
            log.warning("Vysledek %s: %s", mid, exc)
            continue
        match = next((m for m in matches if m["id"] == mid), None)
        if match is None or match["state"] != "post":
            continue
        fav_goals, opp_goals = ((match["home_score"], match["away_score"]) if info["fav"] == "home"
                                else (match["away_score"], match["home_score"]))
        done[mid] = (f"{match['home_score']}:{match['away_score']}",
                     int(fav_goals > opp_goals), int(fav_goals >= opp_goals))
    if done:
        rows = read_cases()
        for row in rows:
            if row["zapas_id"] in done and not row["konec"]:
                row["konec"], row["favorit_vyhral"], row["favorit_neprohral"] = map(str, done[row["zapas_id"]])
        write_cases(rows)
        for mid in done:
            pending.pop(mid, None)
        log.info("Doplneny vysledky: %s", len(done))
    return len(done)


# --------------------------------------------------------------------------- #
# Hlavni smycka
# --------------------------------------------------------------------------- #
def _single_instance() -> socket.socket | None:
    """Zabrany lokalni port = hlidac uz bezi (napr. spusteny planovacem i rucne)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        sock.close()
        return None
    return sock


def run(cfg: Config) -> int:
    lock = _single_instance()
    if lock is None:
        log.info("Hlidac uz bezi, druhou instanci nespoustim.")
        return 0
    state = load_state()
    espn = Espn(cfg)
    odds_api = OddsApi(cfg, state) if cfg.odds_api_key else None
    notifier = TelegramNotifier(cfg) if cfg.telegram_bot_token and cfg.telegram_chat_id else None
    log.info("Hlidac spusten: %s lig, favorite do %.2f, gol do %s. min.",
             len(cfg.hlidac_leagues), cfg.hlidac_max_odds, cfg.hlidac_goal_minute)
    window = timedelta(minutes=cfg.hlidac_window_minutes)
    last_finish = 0.0
    while True:
        try:
            now = datetime.now(cfg.tz)
            day = now.strftime("%Y-%m-%d")
            if state.get("den") != day and now.hour >= cfg.hlidac_prep_hour:
                state["den"], state["kandidati"] = day, plan_day(espn, cfg, now)
                log.info("%s: kandidatu %s", day, len(state["kandidati"]))
                _send(notifier, format_plan(state["kandidati"], cfg))
                save_state(state)
            candidates: dict[str, dict[str, Any]] = state.get("kandidati", {})
            active = {mid: c for mid, c in candidates.items()
                      if c["stav"] in ("ceka", "kurz")
                      and _kickoff(c) - timedelta(minutes=2) <= now <= _kickoff(c) + window}
            if active:
                for league in sorted({c["league"] for c in active.values()}):
                    try:
                        matches = {m["id"]: m for m in espn.scoreboard(league, _kickoff(
                            next(c for c in active.values() if c["league"] == league)).strftime("%Y%m%d"))}
                    except HlidacError as exc:
                        log.warning("%s", exc)
                        continue
                    for mid, cand in active.items():
                        if cand["league"] != league or mid not in matches:
                            continue
                        verdict, info = check_trigger(matches[mid], cand, cfg.hlidac_goal_minute,
                                                      cfg.hlidac_goal_minute + 3)
                        if verdict == "trigger":
                            cand["stav"] = "kurz"
                            if record_case(odds_api, cfg, state, mid, cand, info,
                                           matches[mid]["minute"], notifier):
                                cand["stav"] = "zaznamenano"
                        elif verdict == "pryc":
                            cand["stav"] = "nic"
                            log.info("%s - %s: %s", cand["home"], cand["away"], info)
                for cand in active.values():
                    if cand["stav"] in ("ceka", "kurz") and now > _kickoff(cand) + window - timedelta(minutes=1):
                        cand["stav"] = "nic"
                save_state(state)
                time.sleep(cfg.hlidac_interval_seconds)
                continue
            if time.time() - last_finish > 30 * 60 and state.get("k_dohrani"):
                finish_results(espn, cfg, state)
                save_state(state)
                last_finish = time.time()
            time.sleep(60)
        except KeyboardInterrupt:
            save_state(state)
            log.info("Ukonceno.")
            return 0
        except Exception as exc:  # noqa: BLE001 - hlidac musi prezit cokoli
            log.exception("Chyba: %s", exc)
            save_state(state)
            time.sleep(120)


# --------------------------------------------------------------------------- #
# Vyhodnoceni a diagnostika
# --------------------------------------------------------------------------- #
def report(rows: list[dict[str, str]] | None = None) -> str:
    rows = read_cases() if rows is None else rows
    if not rows:
        return "Zatim zadne pripady."
    out = [f"Zaznamenanych pripadu: {len(rows)}"]
    with_odds = [r for r in rows if r["live_vyhra"]]
    if with_odds:
        ratio = [float(r["live_vyhra"]) / float(r["ferovy_vyhra"]) for r in with_odds]
        out.append(f"Se zivym kurzem na vyhru: {len(with_odds)}   prumerny zivy / ferovy kurz: "
                   f"{sum(ratio) / len(ratio):.2f}x")
        out.append(f"Kurz nad hranici VALUE: {sum(int(r['value_vyhra']) for r in with_odds)} z {len(with_odds)}")
    done = [r for r in with_odds if r["favorit_vyhral"] != ""]
    for label, sel in (("vsechny", done), ("jen VALUE", [r for r in done if r["value_vyhra"] == "1"])):
        if not sel:
            continue
        pnl = [(float(r["live_vyhra"]) - 1) if r["favorit_vyhral"] == "1" else -1 for r in sel]
        wins = sum(r["favorit_vyhral"] == "1" for r in sel)
        out.append(f"Vyhra favorita - {label}: {len(sel)} sazek, favorit vyhral {wins}x "
                   f"({wins / len(sel):.0%}), zisk {sum(pnl):+.2f} j., ROI {sum(pnl) / len(sel):+.1%}")
    out.append("Pozn.: pod ~50 pripady je vysledek hlavne nahoda. "
               "Rozhodujici je, jak casto je zivy kurz nad hranici.")
    return "\n".join(out)


def diag(cfg: Config) -> int:
    espn = Espn(cfg)
    today = datetime.now(cfg.tz)
    problems = 0
    print("Ligy (ESPN, program na dnes a zitra):")
    for league in cfg.hlidac_leagues:
        try:
            n = sum(len(espn.scoreboard(league, (today + timedelta(days=d)).strftime("%Y%m%d")))
                    for d in (0, 1))
            live = "zivy kurz ano" if league in ESPN_TO_ODDS else "zivy kurz NE (chybi v ESPN_TO_ODDS)"
            print(f"  {league:18} OK, zapasu {n:2}  | {live}")
        except HlidacError as exc:
            problems += 1
            print(f"  {league:18} !!! {exc}")
    if cfg.odds_api_key:
        try:
            r = requests.get("https://api.the-odds-api.com/v4/sports", params={"apiKey": cfg.odds_api_key},
                             timeout=cfg.http_timeout)
            print(f"The Odds API: OK, zbyva kreditu {r.headers.get('x-requests-remaining')} (dotaz zdarma)")
        except requests.RequestException as exc:
            problems += 1
            print(f"The Odds API: chyba {exc}")
    else:
        print("The Odds API: chybi ODDS_API_KEY -- pripady se zapisou bez ziveho kurzu.")
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        _send(TelegramNotifier(cfg), "Hlidac: test spojeni OK")
        print("Telegram: testovaci zprava odeslana (zkontroluj mobil).")
    else:
        print("Telegram: neni nastaven (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID v .env).")
    return 1 if problems else 0


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%d.%m. %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stdout is not None:            # pythonw (planovac) nema konzoli
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="Hlidac zivych kurzu (favorit dostane gol do 15. minuty).")
    parser.add_argument("command", choices=("run", "diag", "report", "plan"), nargs="?", default="run")
    parser.add_argument("day", nargs="?", help="pro 'plan': den RRRR-MM-DD (vychozi dnes)")
    args = parser.parse_args(argv)
    _setup_logging()
    if args.command == "report":
        print(report())
        return 0
    try:
        cfg = load_config()
        cfg.validate(require_telegram=False)
    except ConfigError as exc:
        print(f"Chyba konfigurace: {exc}", file=sys.stderr)
        return 2
    if args.command == "plan":
        day = (datetime.strptime(args.day, "%Y-%m-%d").replace(tzinfo=cfg.tz)
               if args.day else datetime.now(cfg.tz))
        print(format_plan(plan_day(Espn(cfg), cfg, day), cfg))
        return 0
    return diag(cfg) if args.command == "diag" else run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
