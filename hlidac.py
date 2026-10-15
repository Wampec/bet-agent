"""
hlidac.py -- hlidac zivych kurzu. Bezi na tvem pocitaci, ne na GitHubu.

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

Smysl prvnich tydnu je OVERIT, jestli kancelare takovy kurz opravdu nabizeji.
Ferove kurzy jsou odhad z historickych dat bez kurzu (sila favorita z Elo),
takze je ber jako hypotezu, kterou zapsane pripady potvrdi nebo vyvrati.

Prikazy:
    python hlidac.py diag      kontrola klice, lig a Telegramu (~3 dotazy)
    python hlidac.py run       hlidani (nechat bezet, Ctrl+C ukonci)
    python hlidac.py report    vyhodnoceni zapsanych pripadu
    python hlidac.py selftest  test logiky bez internetu (soucast selftest.py)

Klic API_FOOTBALL_KEY patri do .env, ostatni nastaveni je v settings.env
(HLIDAC_*). Telegram je stejny jako u denniho agenta.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from config import BASE_DIR, Config, ConfigError, enable_utf8_stdout, load_config
from notifier import TelegramError, TelegramNotifier

log = logging.getLogger("hlidac")

API_URL = "https://v3.football.api-sports.io"
STATE_FILE = BASE_DIR / "hlidac_stav.json"
CASES_FILE = BASE_DIR / "hlidac_pripady.csv"

# Ferove kurzy z historickych dat: (max predzapasovy kurz favorita,
# ferovy kurz na vyhru, ferovy kurz na dvojtip "favorit neprohraje").
#   do 1,40:   po 0:1 do 15. min vyhraje 61,9 %  -> 1,61;  neprohraje 81 % -> 1,23
#   1,40-1,50: vyhraje 48,7 %                    -> 2,05;  neprohraje 71 % -> 1,41
FAIR_ODDS: tuple[tuple[float, float, float], ...] = ((1.40, 1.61, 1.23), (1.50, 2.05, 1.41))

CSV_COLUMNS = [
    "datum", "liga", "domaci", "hoste", "fixture_id", "favorit", "kurz_pred",
    "minuta_golu", "minuta_zaznamu", "live_vyhra", "live_remiza", "live_soupere",
    "live_dvojtip", "ferovy_vyhra", "ferovy_dvojtip", "value_vyhra", "value_dvojtip",
    "konec", "favorit_vyhral", "favorit_neprohral",
]
FINISHED = {"FT", "AET", "PEN"}
NOT_STARTED = {"NS", "TBD"}
DEAD = {"PST", "CANC", "ABD", "AWD", "WO"}
WIN_MARKETS = {"fulltime result", "match winner", "1x2"}


class HlidacError(RuntimeError):
    """Chyba API-Football nebo vycerpany denni limit."""


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
# API-Football
# --------------------------------------------------------------------------- #
class ApiFootball:
    """Tenky klient s dennim limitem dotazu (bezplatny tarif: 100 denne)."""

    def __init__(self, cfg: Config, state: dict[str, Any], http: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self._http = http if http is not None else requests.Session()

    def _budget_ok(self) -> bool:
        today = datetime.now(self.cfg.tz).strftime("%Y-%m-%d")
        if self.state.get("req_den") != today:
            self.state["req_den"], self.state["req_pocet"] = today, 0
        return self.state["req_pocet"] < self.cfg.hlidac_max_requests_per_day

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        if not self._budget_ok():
            raise HlidacError("denni limit dotazu vycerpan (HLIDAC_MAX_REQUESTS_PER_DAY)")
        headers = {"x-apisports-key": self.cfg.api_football_key}
        last_error = ""
        for attempt in range(1, 3):
            try:
                resp = self._http.get(f"{API_URL}/{path}", params=params, headers=headers,
                                      timeout=self.cfg.http_timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(5 * attempt)
                continue
            self.state["req_pocet"] = self.state.get("req_pocet", 0) + 1
            remaining = resp.headers.get("x-ratelimit-requests-remaining")
            if remaining is not None:
                self.state["api_zbyva"] = remaining
            if resp.status_code != 200:
                raise HlidacError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            if data.get("errors"):
                raise HlidacError(f"{path}: {data['errors']}")
            return data
        raise HlidacError(last_error)

    def get_all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get(path, **params) if page == 1 else self.get(path, page=page, **params)
            out += data.get("response", [])
            paging = data.get("paging") or {}
            if paging.get("current", 1) >= paging.get("total", 1):
                return out
            page += 1


# --------------------------------------------------------------------------- #
# Cteni kurzu
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def prematch_1x2(odds_item: dict[str, Any], preferred: list[str]) -> tuple[float, float, float] | None:
    """(domaci, remiza, hoste) z preferovane kancelare, jinak prumer dostupnych."""
    rows: list[tuple[str, float, float, float]] = []
    for bookmaker in odds_item.get("bookmakers", []):
        for bet in bookmaker.get("bets", []):
            if (bet.get("name") or "").lower() not in WIN_MARKETS:
                continue
            values = {v.get("value"): _num(v.get("odd")) for v in bet.get("values", [])}
            if values.get("Home") and values.get("Draw") and values.get("Away"):
                rows.append((bookmaker.get("name", ""), values["Home"], values["Draw"], values["Away"]))
    if not rows:
        return None
    for name in preferred:
        for row in rows:
            if row[0].lower() == name.lower():
                return row[1:]
    count = len(rows)
    return tuple(sum(row[i] for row in rows) / count for i in (1, 2, 3))  # type: ignore[return-value]


def live_odds(item: dict[str, Any], fav: str) -> dict[str, float | None]:
    """Z /odds/live: kurz na vyhru favorita, remizu, soupere a dvojtip favorita."""
    out: dict[str, float | None] = {"win": None, "draw": None, "opp": None, "dc": None}
    fav_key, opp_key = ("Home", "Away") if fav == "home" else ("Away", "Home")
    dc_keys = ("Home/Draw", "Home or Draw", "1X") if fav == "home" else ("Draw/Away", "Draw or Away", "X2")
    for market in item.get("odds", []):
        name = (market.get("name") or "").lower()
        values: dict[str, float | None] = {}
        for v in market.get("values", []):
            if v.get("suspended"):
                continue
            if v.get("handicap") not in (None, "", "0", 0):
                continue
            values[v.get("value")] = _num(v.get("odd"))
        if name in WIN_MARKETS and not out["win"]:
            out["win"], out["draw"], out["opp"] = values.get(fav_key), values.get("Draw"), values.get(opp_key)
        if name == "double chance" and not out["dc"]:
            out["dc"] = next((values[k] for k in dc_keys if values.get(k)), None)
    return out


def fair_for(pre_odds: float) -> tuple[float, float] | tuple[None, None]:
    for max_pre, fair_win, fair_dc in FAIR_ODDS:
        if pre_odds <= max_pre:
            return fair_win, fair_dc
    return None, None


# --------------------------------------------------------------------------- #
# Spoustec
# --------------------------------------------------------------------------- #
def check_trigger(
    fixture: dict[str, Any], cand: dict[str, Any], max_goal_minute: int = 15, max_elapsed: int = 18
) -> tuple[str, Any]:
    """
    ("trigger", minuta_golu) | ("pryc", duvod) | ("cekej", None)
    Pravidlo: favorit prohrava presne 0:1 a gol soupere padl do max_goal_minute.
    """
    status = fixture["fixture"]["status"]
    short, elapsed = status.get("short"), status.get("elapsed") or 0
    if short in NOT_STARTED:
        return "cekej", None
    if short in DEAD:
        return "pryc", "zruseno/odlozeno"
    goals_home = fixture["goals"]["home"] or 0
    goals_away = fixture["goals"]["away"] or 0
    fav_goals, opp_goals = (goals_home, goals_away) if cand["fav"] == "home" else (goals_away, goals_home)
    if fav_goals == 0 and opp_goals == 1:
        opp_id = cand["away_id"] if cand["fav"] == "home" else cand["home_id"]
        minute = None
        for event in fixture.get("events") or []:
            if (event.get("type") == "Goal" and (event.get("team") or {}).get("id") == opp_id
                    and event.get("detail") != "Missed Penalty"):
                minute = (event.get("time") or {}).get("elapsed")
                break
        if minute is None:
            minute = elapsed  # bez udalosti bereme aktualni minutu (konzervativne)
        if minute <= max_goal_minute:
            return "trigger", minute
        return "pryc", f"gol az v {minute}. min"
    if elapsed > max_elapsed or fav_goals > 0 or opp_goals > 1:
        return "pryc", f"{fav_goals}:{opp_goals} v {elapsed}. min"
    return "cekej", None


# --------------------------------------------------------------------------- #
# Denni priprava, zaznam pripadu, vysledky
# --------------------------------------------------------------------------- #
def _send(notifier: TelegramNotifier | None, text: str) -> None:
    if notifier is None:
        log.info("(Telegram nenastaven) %s", text.replace("\n", " | "))
        return
    try:
        notifier.send(text, parse_mode="")
    except TelegramError as exc:
        log.error("Telegram: %s", exc)


def prepare_day(api: ApiFootball, cfg: Config, state: dict[str, Any], day: str,
                notifier: TelegramNotifier | None) -> None:
    leagues = set(cfg.hlidac_leagues)
    fixtures = api.get_all("fixtures", date=day, timezone=cfg.timezone)
    fixtures = [f for f in fixtures
                if f["league"]["id"] in leagues and f["fixture"]["status"]["short"] in NOT_STARTED]
    by_league: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for f in fixtures:
        by_league.setdefault((f["league"]["id"], f["league"]["season"]), []).append(f)

    candidates: dict[str, dict[str, Any]] = {}
    for (league_id, season), items in by_league.items():
        try:
            odds = {o["fixture"]["id"]: o
                    for o in api.get_all("odds", league=league_id, season=season, date=day)}
        except HlidacError as exc:
            log.warning("Kurzy ligy %s: %s", league_id, exc)
            continue
        for f in items:
            item = odds.get(f["fixture"]["id"])
            prices = prematch_1x2(item, cfg.hlidac_bookmakers) if item else None
            if not prices:
                continue
            home, _, away = prices
            fav, pre = ("home", home) if home <= away else ("away", away)
            if pre <= cfg.hlidac_max_odds:
                candidates[str(f["fixture"]["id"])] = {
                    "kickoff": f["fixture"]["date"],
                    "liga": f["league"]["name"], "zeme": f["league"]["country"],
                    "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
                    "home_id": f["teams"]["home"]["id"], "away_id": f["teams"]["away"]["id"],
                    "fav": fav, "pre": round(pre, 2), "stav": "ceka", "pokusy_kurz": 0,
                }
    state["den"], state["kandidati"] = day, candidates
    state.setdefault("k_dohrani", {})
    log.info("%s: %s zapasu ve sledovanych ligach, favoritu do %.2f: %s",
             day, len(fixtures), cfg.hlidac_max_odds, len(candidates))
    if candidates:
        lines = []
        for c in sorted(candidates.values(), key=lambda c: c["kickoff"]):
            when = datetime.fromisoformat(c["kickoff"]).astimezone(cfg.tz).strftime("%H:%M")
            lines.append(f"{when} {c['home']} - {c['away']} (favorit {c[c['fav']]} {c['pre']})")
        _send(notifier, f"Hlidac: dnes {len(candidates)} favoritu do {cfg.hlidac_max_odds}\n"
                        + "\n".join(lines[:40]))


def record_case(api: ApiFootball, cfg: Config, state: dict[str, Any], fid: str,
                cand: dict[str, Any], minute: int, elapsed: int,
                notifier: TelegramNotifier | None) -> bool:
    """Stahne zivy kurz, zapise pripad a posle zpravu. False = kurz jeste neni, zkusit znovu."""
    try:
        live = api.get("odds/live", fixture=fid).get("response", [])
    except HlidacError as exc:
        log.warning("Zivy kurz %s: %s", fid, exc)
        live = []
    odds = live_odds(live[0], cand["fav"]) if live else {"win": None, "draw": None, "opp": None, "dc": None}
    if not odds["win"] and cand["pokusy_kurz"] < 3:
        cand["pokusy_kurz"] += 1          # kurzy byvaji po golu chvili zavrene
        return False

    fair_win, fair_dc = fair_for(cand["pre"])
    margin = cfg.hlidac_value_margin
    value_win = bool(odds["win"] and fair_win and odds["win"] >= fair_win * margin)
    value_dc = bool(odds["dc"] and fair_dc and odds["dc"] >= fair_dc * margin)
    append_case({
        "datum": datetime.now(cfg.tz).strftime("%Y-%m-%d"),
        "liga": f"{cand['zeme']} - {cand['liga']}", "domaci": cand["home"], "hoste": cand["away"],
        "fixture_id": fid, "favorit": cand[cand["fav"]], "kurz_pred": cand["pre"],
        "minuta_golu": minute, "minuta_zaznamu": elapsed,
        "live_vyhra": odds["win"], "live_remiza": odds["draw"], "live_soupere": odds["opp"],
        "live_dvojtip": odds["dc"], "ferovy_vyhra": fair_win, "ferovy_dvojtip": fair_dc,
        "value_vyhra": int(value_win), "value_dvojtip": int(value_dc),
        "konec": "", "favorit_vyhral": "", "favorit_neprohral": "",
    })
    state.setdefault("k_dohrani", {})[fid] = cand["fav"]

    head = "VALUE" if (value_win or value_dc) else "bez value"
    fmt = lambda v: f"{v:.2f}" if v else "-"  # noqa: E731
    _send(notifier,
          f"{head}: {cand['home']} - {cand['away']} 0:1 proti favoritovi ({minute}. min)\n"
          f"Favorit {cand[cand['fav']]}, kurz pred zapasem {cand['pre']}\n"
          f"Zive: vyhra {fmt(odds['win'])} (ferovy {fair_win}, hranice {fair_win * margin:.2f})"
          f" | dvojtip {fmt(odds['dc'])} (ferovy {fair_dc}, hranice {fair_dc * margin:.2f})\n"
          f"Zkontroluj kurz u Tipsportu/Fortuny. Nic se nesazi automaticky.")
    log.info("ZAZNAM %s - %s zive %s", cand["home"], cand["away"], odds)
    return True


def finish_results(api: ApiFootball, state: dict[str, Any]) -> int:
    pending: dict[str, str] = state.get("k_dohrani", {})
    if not pending:
        return 0
    ids = list(pending)[:20]
    fixtures = api.get("fixtures", ids="-".join(ids)).get("response", [])
    done: dict[str, tuple[str, int, int]] = {}
    for f in fixtures:
        if f["fixture"]["status"]["short"] not in FINISHED:
            continue
        fid = str(f["fixture"]["id"])
        gh, ga = f["score"]["fulltime"]["home"], f["score"]["fulltime"]["away"]
        fav_goals, opp_goals = (gh, ga) if pending[fid] == "home" else (ga, gh)
        done[fid] = (f"{gh}:{ga}", int(fav_goals > opp_goals), int(fav_goals >= opp_goals))
    if not done:
        return 0
    rows = read_cases()
    for row in rows:
        if row["fixture_id"] in done and not row["konec"]:
            row["konec"], row["favorit_vyhral"], row["favorit_neprohral"] = map(str, done[row["fixture_id"]])
    write_cases(rows)
    for fid in done:
        pending.pop(fid, None)
    log.info("Doplneny vysledky: %s", len(done))
    return len(done)


# --------------------------------------------------------------------------- #
# Hlavni smycka
# --------------------------------------------------------------------------- #
def run(cfg: Config) -> int:
    state = load_state()
    api = ApiFootball(cfg, state)
    notifier = TelegramNotifier(cfg) if cfg.telegram_bot_token and cfg.telegram_chat_id else None
    _send(notifier, f"Hlidac spusten. Sleduji {len(cfg.hlidac_leagues)} lig, "
                    f"favorite do {cfg.hlidac_max_odds}, gol do {cfg.hlidac_goal_minute}. min.")
    last_finish = 0.0
    window = timedelta(minutes=cfg.hlidac_window_minutes)
    while True:
        try:
            now = datetime.now(cfg.tz)
            day = now.strftime("%Y-%m-%d")
            if state.get("den") != day and now.hour >= cfg.hlidac_prep_hour:
                prepare_day(api, cfg, state, day, notifier)
                save_state(state)
            candidates: dict[str, dict[str, Any]] = state.get("kandidati", {})
            active = [
                fid for fid, c in candidates.items()
                if c["stav"] in ("ceka", "kurz")
                and datetime.fromisoformat(c["kickoff"]) - timedelta(minutes=2) <= now
                <= datetime.fromisoformat(c["kickoff"]) + window
            ]
            if active:
                for start in range(0, len(active), 20):
                    batch = active[start:start + 20]
                    for f in api.get("fixtures", ids="-".join(batch)).get("response", []):
                        fid = str(f["fixture"]["id"])
                        cand = candidates[fid]
                        verdict, info = check_trigger(f, cand, cfg.hlidac_goal_minute,
                                                      cfg.hlidac_goal_minute + 3)
                        if verdict == "trigger":
                            cand["stav"] = "kurz"
                            if record_case(api, cfg, state, fid, cand, info,
                                           f["fixture"]["status"].get("elapsed") or 0, notifier):
                                cand["stav"] = "zaznamenano"
                        elif verdict == "pryc" and cand["stav"] in ("ceka", "kurz"):
                            cand["stav"] = "nic"
                            log.info("%s - %s: %s", cand["home"], cand["away"], info)
                for fid in active:
                    cand = candidates[fid]
                    if (cand["stav"] in ("ceka", "kurz")
                            and now > datetime.fromisoformat(cand["kickoff"]) + window - timedelta(minutes=1)):
                        cand["stav"] = "nic"
                save_state(state)
                time.sleep(cfg.hlidac_interval_seconds)
                continue
            if time.time() - last_finish > 3 * 3600 and state.get("k_dohrani"):
                finish_results(api, state)
                save_state(state)
                last_finish = time.time()
            time.sleep(60)
        except KeyboardInterrupt:
            save_state(state)
            log.info("Ukonceno.")
            return 0
        except HlidacError as exc:
            log.error("API-Football: %s", exc)
            save_state(state)
            time.sleep(120)
        except Exception as exc:  # noqa: BLE001 - hlidac musi prezit cokoli
            log.exception("Neocekavana chyba: %s", exc)
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
    for label, rows_sel in (("vsechny", done), ("jen VALUE", [r for r in done if r["value_vyhra"] == "1"])):
        if not rows_sel:
            continue
        pnl = [(float(r["live_vyhra"]) - 1) if r["favorit_vyhral"] == "1" else -1 for r in rows_sel]
        wins = sum(r["favorit_vyhral"] == "1" for r in rows_sel)
        out.append(f"Vyhra favorita - {label}: {len(rows_sel)} sazek, favorit vyhral {wins}x "
                   f"({wins / len(rows_sel):.0%}), zisk {sum(pnl):+.2f} j., ROI {sum(pnl) / len(rows_sel):+.1%}")
    out.append("Pozn.: pod ~50 pripady je vysledek hlavne nahoda. "
               "Rozhodujici je, jak casto je zivy kurz nad hranici.")
    return "\n".join(out)


def diag(cfg: Config) -> int:
    state = load_state()
    api = ApiFootball(cfg, state)
    status = api.get("status").get("response", {})
    sub, req = status.get("subscription") or {}, status.get("requests") or {}
    print(f"Ucet: {sub.get('plan')} | dnes pouzito: {req.get('current')} / {req.get('limit_day')}")
    leagues = api.get("leagues", current="true").get("response", [])
    names = {item["league"]["id"]: f"{item['country']['name']} - {item['league']['name']}" for item in leagues}
    problems = 0
    for league_id in cfg.hlidac_leagues:
        name = names.get(league_id)
        if name is None:
            problems += 1
        print(f"  id {league_id:4} -> {name or '!!! NENALEZENO - oprav HLIDAC_LEAGUES'}")
    save_state(state)
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        _send(TelegramNotifier(cfg), "Hlidac: test spojeni OK")
        print("Telegram: testovaci zprava odeslana (zkontroluj mobil).")
    else:
        print("Telegram: neni nastaven (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID v .env).")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdout()
    parser = argparse.ArgumentParser(description="Hlidac zivych kurzu (favorit dostane gol do 15. minuty).")
    parser.add_argument("command", choices=("run", "diag", "report", "selftest"), nargs="?", default="run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%d.%m. %H:%M:%S")
    if args.command == "selftest":
        import selftest
        selftest.test_hlidac()
        return 1 if selftest.FAILURES else 0
    if args.command == "report":
        print(report())
        return 0
    try:
        cfg = load_config()
        cfg.validate(require_telegram=False)
    except ConfigError as exc:
        print(f"Chyba konfigurace: {exc}", file=sys.stderr)
        return 2
    if not cfg.api_football_key:
        print("Chybi API_FOOTBALL_KEY v .env (klic z dashboard.api-football.com).", file=sys.stderr)
        return 2
    return diag(cfg) if args.command == "diag" else run(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
