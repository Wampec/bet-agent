"""
Favorit dostane prvni gol od soupere v minute M -- jak casto presto vyhraje?

Data:
  - minuty golu: github.com/schochastics/football-data (data/goals_time)
  - predzapasove kurzy: football-data.co.uk (Bet365 / Pinnacle / prumer)
Sezony 2012/13 - 2024/25, ligy, kde jsou oba zdroje.
"""
import csv
import difflib
import io
import math
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE = HERE / "cache"   # stazena data (mimo repozitar)
CACHE.mkdir(exist_ok=True)
GOALS_URL = "https://raw.githubusercontent.com/schochastics/football-data/master/data/goals_time/{}.csv"

# schochastics soubor -> football-data.co.uk (hlavni ligy: kod, nove ligy: soubor)
MAIN = {
    "eng-premier-league": "E0", "esp-primera-division": "SP1", "bundesliga": "D1",
    "ita-serie-a": "I1", "fra-ligue-1": "F1", "ned-eredivisie": "N1",
    "por-primeira-liga": "P1", "tur-sueperlig": "T1", "sco-premiership": "SC0",
    "bel-pro-league": "B1", "gre-super-league": "G1",
}
NEW = {"aut-bundesliga": "AUT", "sui-super-league": "SWZ", "nor-eliteserien": "NOR",
       "pol-ekstraklasa": "POL"}
SEASONS = [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(2012, 2025)]


def fetch(url: str, name: str) -> str:
    path = CACHE / name
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                text = r.content.decode("utf-8", errors="replace").lstrip("﻿")
                path.write_text(text, encoding="utf-8")
                return text
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    print(f"  !! nepodarilo se stahnout {url}", file=sys.stderr)
    return ""


NOISE = {"fc", "afc", "cf", "sc", "ac", "as", "ssc", "sv", "fk", "bk", "if", "cd", "ud", "rc",
         "rcd", "club", "de", "the", "calcio", "sk", "vfl", "vfb", "tsg", "1", "kv", "krc",
         "rsc", "sporting", "fv", "ks", "aek", "paok", "bv", "borussia"}


def norm(name: str) -> str:
    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ")
    return " ".join(w for w in re.split(r"[^a-z0-9]+", t) if w and w not in NOISE)


def sim(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.95
    return difflib.SequenceMatcher(None, na, nb).ratio()


def parse_date(raw: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            pass
    return None


def fnum(row, *keys):
    for k in keys:
        try:
            v = float(row.get(k) or "")
            if v > 1:
                return v
        except ValueError:
            pass
    return None


def odds_rows(league: str):
    """(datum, domaci, hoste, kurz_d, kurz_h, goly_d, goly_h)"""
    out = []
    if league in MAIN:
        for s in SEASONS:
            text = fetch(f"https://www.football-data.co.uk/mmz4281/{s}/{MAIN[league]}.csv",
                         f"{MAIN[league]}_{s}.csv")
            for r in csv.DictReader(io.StringIO(text)):
                d = parse_date(r.get("Date", ""))
                h = fnum(r, "PSCH", "AvgCH", "B365CH", "PSH", "AvgH", "BbAvH", "B365H")
                a = fnum(r, "PSCA", "AvgCA", "B365CA", "PSA", "AvgA", "BbAvA", "B365A")
                if d and h and a and r.get("FTHG", "").strip().isdigit():
                    out.append((d, r["HomeTeam"], r["AwayTeam"], h, a, int(r["FTHG"]), int(r["FTAG"])))
    else:
        text = fetch(f"https://www.football-data.co.uk/new/{NEW[league]}.csv", f"{NEW[league]}.csv")
        for r in csv.DictReader(io.StringIO(text)):
            d = parse_date(r.get("Date", ""))
            if not d or d.year < 2012:
                continue
            h = fnum(r, "PSCH", "AvgCH", "B365CH", "MaxCH")
            a = fnum(r, "PSCA", "AvgCA", "B365CA", "MaxCA")
            if h and a and r.get("HG", "").strip().isdigit():
                out.append((d, r["Home"], r["Away"], h, a, int(r["HG"]), int(r["AG"])))
    return out


GAME_RE = re.compile(r"^(.*) vs\. (.*) (\d+):(\d+)$")


def goal_games(league: str):
    """{datum: [(domaci, hoste, [(minuta, tym_skoroval)])]}"""
    text = fetch(GOALS_URL.format(league), f"goals_{league}.csv")
    games = defaultdict(list)
    for r in csv.DictReader(io.StringIO(text)):
        m = GAME_RE.match(r["game"].strip())
        d = parse_date(r["date"])
        if not m or not d or d.year < 2012:
            continue
        mt = re.match(r"(\d+)", str(r["time"]))
        if not mt:
            continue
        games[(d, m.group(1), m.group(2))].append((int(mt.group(1)), r["scoring_team"]))
    by_date = defaultdict(list)
    for (d, h, a), goals in games.items():
        by_date[d].append((h, a, sorted(goals)))
    return by_date


def main():
    cases = []          # (kurz_fav, minuta_prvniho_golu_soupere, fav_vyhral, fav_neprohral, liga, sezona_rok)
    fav_matches = defaultdict(int)     # pocet zapasu s favoritem do 1,50 podle ligy
    matched = unmatched = 0
    for league in list(MAIN) + list(NEW):
        if league.startswith("__"):
            continue
        rows = odds_rows(league)
        goals = goal_games(league)
        lm = lu = 0
        for d, home, away, oh, oa, gh, ga in rows:
            fav = "home" if oh <= oa else "away"
            pre = min(oh, oa)
            if pre > 1.50:
                continue
            fav_matches[league] += 1
            if pre <= 1.40:
                fav_matches["__140"] += 1
            if gh + ga == 0:
                lm += 1
                continue                       # 0:0 -- souper prvni gol nedal
            best, best_s = None, 0.0
            for dd in (d, d - timedelta(days=1), d + timedelta(days=1)):
                for gh_name, ga_name, glist in goals.get(dd, []):
                    s = (sim(home, gh_name) + sim(away, ga_name)) / 2
                    if s > best_s:
                        best, best_s = (gh_name, ga_name, glist), s
            if best is None or best_s < 0.6:
                lu += 1
                continue
            gh_name, ga_name, glist = best
            lm += 1
            first_min, first_team = glist[0]
            fav_name, opp_name = (gh_name, ga_name) if fav == "home" else (ga_name, gh_name)
            opp_first = sim(first_team, opp_name) > sim(first_team, fav_name)
            if not opp_first:
                continue
            fg, og = (gh, ga) if fav == "home" else (ga, gh)
            cases.append((pre, first_min, fg > og, fg >= og, league, d.year))
        matched += lm
        unmatched += lu
        print(f"  {league:22} zapasu s favoritem do 1,50: {fav_matches[league]:5}  sparovano {lm / max(1, lm + lu):5.0%}",
              file=sys.stderr)

    print(f"\nZapasu s favoritem do 1,50: {sum(fav_matches.values())}, sparovano s minutami: "
          f"{matched / (matched + unmatched):.0%}")
    print(f"Pripadu 'souper dal prvni gol': {len(cases)}\n")

    def row(label, sel):
        n = len(sel)
        if n == 0:
            print(f"  {label:26}    -")
            return
        w = sum(1 for c in sel if c[2]) / n
        nl = sum(1 for c in sel if c[3]) / n
        se = math.sqrt(w * (1 - w) / n)
        print(f"  {label:26} {n:5}   vyhra {w:5.1%} (±{1.96 * se:4.1%})   neprohra {nl:5.1%}   "
              f"ferovy kurz vyhra {1 / w if w else 0:5.2f}  dvojtip {1 / nl if nl else 0:5.2f}")

    for lo, hi, label in ((1.0, 1.40, "FAVORIT DO 1,40"), (1.0, 1.25, "  z toho do 1,25"),
                          (1.25, 1.40, "  z toho 1,26-1,40"), (1.40, 1.50, "pro srovnani 1,41-1,50")):
        sel = [c for c in cases if lo < c[0] <= hi]
        print(f"\n{label} -- souper dal prvni gol v minute:")
        for a, b in ((1, 10), (11, 15), (16, 20), (21, 25), (26, 30), (31, 45), (46, 90)):
            row(f"{a:2}.-{b:2}. min", [c for c in sel if a <= c[1] <= b])
        print("  " + "-" * 40)
        for b in (15, 20, 25, 30):
            row(f"souhrnne do {b}. min", [c for c in sel if c[1] <= b])

    n140 = fav_matches["__140"]
    print(f"\nCetnost: zapasu s favoritem do 1,40 celkem {n140}; souper da prvni gol do minuty M:")
    for b in (15, 20, 25, 30):
        k = sum(1 for c in cases if c[0] <= 1.40 and c[1] <= b)
        print(f"  do {b:2}. min: {k:4} pripadu = {k / n140:5.1%} zapasu s favoritem")
    return cases, fav_matches


if __name__ == "__main__":
    main()
