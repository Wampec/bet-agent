"""
selftest.py -- offline kontrola bez API klice a bez internetu.

Spusteni:  python selftest.py

Overi de-vigging, konsensus kancelari, sestaveni a razeni tipu, kontroly
duveryhodnosti odhadu, denik tipu, vyhodnoceni vysledku, vyber souteci
(spotrebu kreditu) a sestaveni telegramove zpravy.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import analyzer
from config import Config, ConfigError, enable_utf8_stdout, load_config
from fetcher import Event, OddsAPIClient, merge_markets
from notifier import esc, format_quota_warning, format_tips
from state import (
    STATUS_LOST,
    STATUS_OPEN,
    STATUS_VOID,
    STATUS_WON,
    SentTip,
    TipLog,
    evaluate_score,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def make_cfg(**overrides) -> Config:
    base = dict(
        odds_api_key="test",
        telegram_bot_token="test",
        telegram_chat_id="1",
        state_file=Path("tips.selftest.json"),
    )
    base.update(overrides)
    return Config(**base)


def make_event(event_id="evt-1", sport_key="tennis_atp_us_open", group="Tennis",
               home="Alcaraz", away="Qualifier", hours=6.0) -> Event:
    return Event(
        event_id=event_id,
        sport_key=sport_key,
        sport_title="Test soutez",
        sport_group=group,
        commence_time=datetime.now(timezone.utc) + timedelta(hours=hours),
        home_team=home,
        away_team=away,
    )


def add_market(event: Event, bookmaker: str, market: str,
               outcomes: dict[str, float]) -> None:
    merge_markets(event, {
        "bookmakers": [{
            "title": bookmaker,
            "markets": [{
                "key": market,
                "outcomes": [{"name": k, "price": v} for k, v in outcomes.items()],
            }],
        }]
    })


def two_way_event(event_id="evt-1", fav="Alcaraz", dog="Qualifier",
                  prices=(1.10, 1.11, 1.12, 1.13), dog_price=8.0,
                  sport_key="tennis_atp_us_open", group="Tennis",
                  hours=6.0) -> Event:
    event = make_event(event_id, sport_key, group, fav, dog, hours)
    for index, price in enumerate(prices):
        add_market(event, f"Kancelar {index}", "h2h", {fav: price, dog: dog_price})
    return event


def three_way_event(event_id="hb-1", fav="THW Kiel", dog="TSV Hannover",
                    fav_prices=(1.25, 1.30, 1.25, 1.28), dog_price=4.6,
                    draw_price=10.0, sport_key="handball_germany_bundesliga",
                    group="Handball", hours=8.0) -> Event:
    """Hazena: trojcestny trh, remiza je prohrana sazka."""
    event = make_event(event_id, sport_key, group, fav, dog, hours)
    for index, price in enumerate(fav_prices):
        add_market(event, f"Kancelar {index}", "h2h",
                   {fav: price, dog: dog_price, "Draw": draw_price})
    return event


def soccer_event() -> Event:
    event = make_event("soc-1", "soccer_epl", "Soccer", "Arsenal", "Luton")
    for name, home, draw, away in (
        ("A", 1.36, 5.25, 9.5), ("B", 1.38, 5.00, 9.0),
        ("C", 1.35, 5.40, 9.8), ("D", 1.40, 5.00, 9.2),
    ):
        add_market(event, name, "h2h",
                   {"Arsenal": home, "Draw": draw, "Luton": away})
    for name, p1x, p12, px2 in (
        ("A", 1.06, 1.17, 3.46), ("B", 1.07, 1.19, 3.31),
        ("C", 1.06, 1.17, 3.54), ("D", 1.08, 1.19, 3.25),
    ):
        add_market(event, name, "double_chance",
                   {"Arsenal/Draw": p1x, "Arsenal/Luton": p12, "Draw/Luton": px2})
    for name, price in (("A", 1.10), ("B", 1.11), ("C", 1.10), ("D", 1.12)):
        add_market(event, name, "draw_no_bet", {"Arsenal": price, "Luton": 7.0})
    return event


def sent_tip(event_id="evt-1", market="h2h", prob=0.92, odds=1.12,
             home="Alcaraz", away="Qualifier", pick="Alcaraz",
             hours_ago=6.0) -> SentTip:
    start = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return SentTip(
        sent_at=start.isoformat(), event_id=event_id,
        sport_key="tennis_atp_us_open", sport_title="US Open",
        commence_time=start.isoformat(), home_team=home, away_team=away,
        market=market, selection=pick, pick_team=pick, prob=prob, odds=odds,
    )


# --------------------------------------------------------------------------- #
def test_devig() -> None:
    print("\n[1] De-vigging")
    odds = {"A": 1.50, "B": 4.00, "C": 7.00}
    booksum = sum(1 / o for o in odds.values())
    check("marze kancelare je kladna", booksum > 1.0, f"(booksum={booksum:.4f})")

    for method in ("multiplicative", "shin", "power"):
        fair = analyzer.devig(odds, method)
        total = sum(fair.values())
        check(f"{method}: soucet pravdepodobnosti = 1",
              math.isclose(total, 1.0, abs_tol=1e-6), f"(={total:.6f})")
        check(f"{method}: poradi zustava zachovano", fair["A"] > fair["B"] > fair["C"])
        check(f"{method}: fair prob je nizsi nez implikovana",
              fair["A"] < 1 / odds["A"])

    shin = analyzer.devig(odds, "shin")
    mult = analyzer.devig(odds, "multiplicative")
    check("Shin ubira vic outsiderovi nez proporcionalni metoda",
          shin["C"] < mult["C"], f"(shin={shin['C']:.4f}, mult={mult['C']:.4f})")

    dc = analyzer.devig({"1X": 1.10, "12": 1.30, "X2": 2.60}, "shin", target_sum=2.0)
    check("dvojita sance se normuje na soucet 2",
          math.isclose(sum(dc.values()), 2.0, abs_tol=1e-6))

    check("odstraneni marze snizi sanci pod 1/kurz",
          analyzer.devig({"A": 1.12, "B": 8.0}, "shin")["A"] < 1 / 1.12)


def test_two_way_tip() -> None:
    print("\n[2] Sestaveni tipu z dvoucestneho trhu")
    cfg = make_cfg()
    rejections: list[analyzer.Rejection] = []
    tip = analyzer.build_h2h_tip(two_way_event(), cfg, rejections)
    check("tip vznikl", tip is not None, f"({[r.reason for r in rejections]})")
    if tip is None:
        return
    check("vybran favorit", tip.selection == "Alcaraz")
    check("sance odpovida konsensu", 0.85 < tip.prob < 0.95, f"(={tip.prob:.4f})")
    check("kurz je nejlepsi nalezeny", math.isclose(tip.odds, 1.13, abs_tol=1e-9),
          f"(={tip.odds})")
    check("pocet kancelari sedi", tip.n_books == 4, f"(={tip.n_books})")
    check("shoda je popsana slovem", tip.agreement in {"vysoka", "stredni", "nizka"})
    check("tip prosel kontrolami", analyzer.check_tip(tip, cfg) is None,
          f"({analyzer.check_tip(tip, cfg)})")


def test_no_odds_filter() -> None:
    print("\n[3] Kurz uz nic nefiltruje")
    cfg = make_cfg()
    # Velmi kratky kurz (drive by vypadl pod ODDS_MIN) i dlouhy (nad ODDS_MAX).
    short = analyzer.analyze([two_way_event("a", "Jisty", "Slabsi",
                                            prices=(1.02, 1.03, 1.03, 1.04),
                                            dog_price=20.0)], cfg)
    check("velmi kratky kurz projde", short.best is not None)
    if short.best:
        check("a ma vysokou sanci", short.best.prob > 0.95,
              f"(={short.best.prob:.4f})")

    long_odds = analyzer.analyze([two_way_event("b", "Mirny", "Soupermild",
                                                prices=(1.60, 1.62, 1.61, 1.63),
                                                dog_price=2.3)], cfg)
    check("dlouhy kurz taky projde", long_odds.best is not None)
    if long_odds.best:
        check("ale ma nizsi sanci", long_odds.best.prob < 0.70,
              f"(={long_odds.best.prob:.4f})")


def test_ranking() -> None:
    print("\n[4] Razeni podle sance")
    cfg = make_cfg()
    events = [
        two_way_event("a", "Stredni", "S1", prices=(1.30,) * 4, dog_price=3.6),
        two_way_event("b", "Nejjistejsi", "S2", prices=(1.05,) * 4, dog_price=12.0),
        two_way_event("c", "Druhy", "S3", prices=(1.15,) * 4, dog_price=6.0),
        two_way_event("d", "Nejhorsi", "S4", prices=(1.90,) * 4, dog_price=1.95),
    ]
    result = analyzer.analyze(events, cfg)
    check("vsechny zapasy daly tip", len(result.tips) == 4, f"({len(result.tips)})")
    order = [t.selection for t in result.tips]
    check("razeno od nejvyssi sance",
          order == ["Nejjistejsi", "Druhy", "Stredni", "Nejhorsi"], f"({order})")
    check("sance klesa",
          all(result.tips[i].prob >= result.tips[i + 1].prob
              for i in range(len(result.tips) - 1)))
    check("best vraci nejjistejsi", result.best.selection == "Nejjistejsi")


def test_bands() -> None:
    print("\n[4b] Kurzova pasma")
    bands = ((1.12, 1.24), (1.25, 1.35))

    def band(odds: float) -> str:
        return analyzer.band_for_odds(odds, bands)

    check("1,12 je v pasmu A", band(1.12) == "A")
    check("1,24 je v pasmu A", band(1.24) == "A")
    check("1,25 je v pasmu B", band(1.25) == "B")
    check("1,35 je v pasmu B", band(1.35) == "B")
    check("1,11 je tichy zbytek pod", band(1.11) == analyzer.REST_BELOW)
    check("1,36 je tichy zbytek nad", band(1.36) == analyzer.REST_ABOVE)
    check("median 1,245 se zaokrouhli nahoru do B", band(1.245) == "B")
    check("median 1,115 se zaokrouhli do A", band(1.115) == "A")
    check("median 1,244 zustane v A", band(1.244) == "A")
    check("mezera mezi pasmy se pozna",
          analyzer.band_for_odds(1.30, ((1.12, 1.24), (1.40, 1.50)))
          == analyzer.REST_BETWEEN)

    open_bands = ((1.15, 1.19), (1.20, 1.24), (1.25, math.inf))
    check("pasmo bez horni hranice: 1,25 je v C",
          analyzer.band_for_odds(1.25, open_bands) == "C")
    check("pasmo bez horni hranice: 1,60 je v C",
          analyzer.band_for_odds(1.60, open_bands) == "C")
    check("pasmo bez horni hranice: 4,00 je v C",
          analyzer.band_for_odds(4.00, open_bands) == "C")
    check("1,14 je pod nejnizsim pasmem",
          analyzer.band_for_odds(1.14, open_bands) == analyzer.REST_BELOW)
    check("median 1,195 se zaokrouhli do B",
          analyzer.band_for_odds(1.195, open_bands) == "B")
    check("popis pasma bez horni hranice",
          analyzer.band_range_text(1.25, math.inf) == "1,25 a vys")
    check("popis bezneho pasma",
          analyzer.band_range_text(1.15, 1.19) == "1,15–1,19")

    # Median, ne nejlepsi kurz: burza nabizi 1,30, bezne kancelare 1,22.
    event = two_way_event("m", "Favorit", "Outsider",
                          prices=(1.21, 1.22, 1.22, 1.23, 1.30), dog_price=4.5)
    tip = analyzer.analyze([event], make_cfg()).best
    check("nejlepsi kurz je z burzy", tip is not None and tip.odds == 1.30)
    check("bezny kurz je median", tip is not None and tip.median_odds == 1.22,
          f"({tip.median_odds if tip else None})")
    check("do pasma rozhoduje bezny kurz",
          tip is not None and band(tip.typical_odds) == "A")

    cfg = make_cfg(tips_per_band=3, odds_bands=bands)
    events = []
    for index, price in enumerate((1.14, 1.15, 1.16, 1.18, 1.20,   # A (5 zapasu)
                                    1.26, 1.28,                     # B (2 zapasy)
                                    1.05, 1.50)):                   # pod, nad
        events.append(two_way_event(f"e{index}", f"Fav{index}", f"Dog{index}",
                                    prices=(price,) * 4, dog_price=7.0))
    result = analyzer.analyze(events, cfg)
    selected = analyzer.select_by_band(result.tips, cfg)
    check("pasmo A ma nejvys 3 tipy", len(selected["A"]) == 3, f"({len(selected['A'])})")
    check("v pasmu A jsou 3 nejvyssi sance",
          [t.selection for t in selected["A"]] == ["Fav0", "Fav1", "Fav2"],
          f"({[t.selection for t in selected['A']]})")
    check("pasmo B vezme, co ma (2)", len(selected["B"]) == 2)
    check("tichy zbytek pod", [t.selection for t in selected["pod"]] == ["Fav7"])
    check("tichy zbytek nad", [t.selection for t in selected["nad"]] == ["Fav8"])
    check("kazdy tip je prave v jednom pasmu",
          sum(len(v) for v in selected.values()) == 7)


def test_band_stats() -> None:
    print("\n[4c] Statistiky pasem a virtualni zisk")
    tlog = TipLog(path=Path("tips.selftest.json"))
    won = sent_tip("w", odds=1.25)
    won.median_odds, won.band, won.status, won.prob = 1.20, "A", STATUS_WON, 0.85
    lost = sent_tip("l", odds=1.22)
    lost.median_odds, lost.band, lost.status, lost.prob = 1.20, "A", STATUS_LOST, 0.83
    void = sent_tip("v", odds=1.20)
    void.band, void.status = "A", STATUS_VOID
    waiting = sent_tip("o", odds=1.18)
    waiting.band = "A"
    other = sent_tip("b", odds=1.30)
    other.band, other.status = "B", STATUS_WON
    tlog.tips = [won, lost, void, waiting, other]

    stats = tlog.band_stats("A", 100.0)
    check("pocita jen tipy daneho pasma", stats.decided == 2, f"({stats.decided})")
    check("vyhry", stats.wins == 1)
    check("void se nepocita", stats.decided == 2)
    check("otevrene tipy se hlasi jako cekajici", stats.pending == 1)
    check("uspesnost 50 %", stats.rate == 0.5)
    check("ocekavana uspesnost je prumer predpovedi",
          math.isclose(stats.expected, 0.84, abs_tol=1e-9), f"({stats.expected})")
    check("virtualni zisk pocita s beznym kurzem: +20 - 100 = -80",
          math.isclose(stats.profit, -80.0, abs_tol=1e-9), f"({stats.profit})")

    empty = tlog.band_stats("C", 100.0)
    check("prazdne pasmo nema uspesnost", empty.rate is None and empty.expected is None)

    old = sent_tip("old", odds=1.27)
    old.band = None
    legacy = TipLog(path=Path("tips.selftest.json"), tips=[old])
    count = legacy.reassign_bands(
        lambda odds: analyzer.band_for_odds(odds, ((1.12, 1.24), (1.25, 1.35))))
    check("stary zaznam bez pasma se zaradi", count == 1 and old.band == "B")
    check("beze zmeny pasem se nic neprerazuje",
          legacy.reassign_bands(
              lambda odds: analyzer.band_for_odds(odds, ((1.12, 1.24), (1.25, 1.35))))
          == 0)
    check("po zmene pasem se historie roztridi znovu",
          legacy.reassign_bands(
              lambda odds: analyzer.band_for_odds(
                  odds, ((1.15, 1.19), (1.20, 1.24), (1.25, math.inf)))) == 1
          and old.band == "C")
    old.band = "B"   # zbytek testu pocita s puvodnim zarazenim

    fresh = sent_tip("old", odds=1.27)
    fresh.median_odds, fresh.band, fresh.shown, fresh.n_outcomes = 1.21, "A", True, 2
    legacy.add([fresh], "2026-09-14")
    check("znovu nalezeny stary zaznam dostane bezny kurz a opravene pasmo",
          len(legacy.tips) == 1 and old.median_odds == 1.21 and old.band == "A")

    settled_new = sent_tip("new", odds=1.30)
    settled_new.median_odds, settled_new.band = 1.28, "B"
    legacy.add([settled_new], "2026-09-14")
    again = sent_tip("new", odds=1.40)
    again.median_odds, again.band = 1.38, "nad"
    legacy.add([again], "2026-09-14")
    check("zaznam, ktery uz bezny kurz ma, se neprepisuje",
          legacy.tips[1].band == "B" and legacy.tips[1].median_odds == 1.28)


def test_reliability_checks() -> None:
    print("\n[5] Kontroly duveryhodnosti odhadu")
    cfg = make_cfg()

    few = analyzer.analyze([two_way_event("a", prices=(1.10, 1.11))], cfg)
    check("malo kancelari se vyradi", not few.tips)
    check("duvod je oznacen",
          any(r.stage == analyzer.STAGE_BOOKMAKERS for r in few.rejections),
          f"({[(r.stage, r.reason) for r in few.rejections]})")

    # Kancelare se rozchazeji v odhadu vic, nez dovoluje prah.
    spread = two_way_event("b", prices=(1.05, 1.35, 1.10, 1.45), dog_price=6.0)
    tight = make_cfg(max_prob_dispersion=0.02)
    messy = analyzer.analyze([spread], tight)
    check("velky nesoulad kancelari se vyradi", not messy.tips,
          f"({[t.dispersion for t in messy.tips]})")
    check("duvod je oznacen",
          any(r.stage == analyzer.STAGE_DISAGREEMENT for r in messy.rejections))
    check("shodne kancelare projdou i pri prisnem prahu",
          len(analyzer.analyze([two_way_event("b2")], tight).tips) == 1)

    check("vychozi odstup pred zacatkem je 0", cfg.min_minutes_before_start == 0)
    soon = analyzer.analyze([two_way_event("c", hours=0.2)], cfg)
    check("zapas za 12 minut se ve vychozim nastaveni posle", len(soon.tips) == 1,
          f"({[r.reason for r in soon.rejections]})")
    started = analyzer.analyze([two_way_event("c2", hours=-0.5)], cfg)
    check("uz zacaty zapas se porad nenabizi", not started.tips)
    check("duvod je oznacen",
          any(r.stage == analyzer.STAGE_TIME for r in started.rejections))

    buffered = make_cfg(min_minutes_before_start=45)
    late = analyzer.analyze([two_way_event("c3", hours=0.2)], buffered)
    check("s odstupem 45 min se zapas tesne pred zacatkem vyradi", not late.tips)
    check("duvod je oznacen i u odstupu",
          any(r.stage == analyzer.STAGE_TIME for r in late.rejections))

    from fetcher import day_window
    now = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    start, _ = day_window(cfg, now)
    check("okno dotazu zacina hned (bez odstupu)", start == now, f"({start})")
    start45, _ = day_window(buffered, now)
    check("okno dotazu respektuje odstup, kdyz je nastaveny",
          start45 == now + timedelta(minutes=45))

    strict = make_cfg(min_tip_prob=0.90)
    weak = analyzer.analyze([two_way_event("e", prices=(1.30,) * 4, dog_price=3.6)],
                            strict)
    check("MIN_TIP_PROB vyradi slabe tipy", not weak.tips)
    check("duvod je oznacen",
          any(r.stage == analyzer.STAGE_LOW_PROB for r in weak.rejections))


def test_three_way_markets() -> None:
    print("\n[5b] Trojcestny trh (hazena)")
    cfg = make_cfg()
    result = analyzer.analyze([three_way_event()], cfg)
    check("hazena da tip", result.best is not None,
          f"({[(r.stage, r.reason) for r in result.rejections]})")
    if result.best is None:
        return
    tip = result.best
    check("tip je na favorita, ne na remizu", tip.selection == "THW Kiel")
    check("trh je oznacen jako trojcestny", tip.has_draw and tip.n_outcomes == 3)
    check("popis rika, ze remiza je prohra", "remiza" in tip.market_label.lower())
    check("sance je nizsi nez 1/kurz kvuli remize",
          tip.prob < 1 / tip.odds, f"(p={tip.prob:.3f}, 1/kurz={1 / tip.odds:.3f})")
    check("sance je v rozumnem rozsahu", 0.65 < tip.prob < 0.85,
          f"(={tip.prob:.3f})")

    strict = make_cfg(two_way_only=True)
    check("TWO_WAY_ONLY=true hazenu vyradi",
          not analyzer.analyze([three_way_event()], strict).tips)

    # Remiza jako nejpravdepodobnejsi vysledek se nesmi stat tipem.
    even = three_way_event("hb-2", "Tym A", "Tym B",
                           fav_prices=(3.4, 3.5, 3.45, 3.4), dog_price=3.5,
                           draw_price=2.6)
    even_result = analyzer.analyze([even], cfg)
    check("remiza se nikdy nenabizi jako tip",
          even_result.best is None or even_result.best.selection != "Draw",
          f"({even_result.best.selection if even_result.best else None})")


def test_mixed_market_structures() -> None:
    print("\n[5c] Michani dvoucestnych a trojcestnych nabidek")
    cfg = make_cfg()
    event = three_way_event()
    # Jedna kancelar kotuje dvoucestne (jinou sazku) -- nesmi zkreslit konsensus.
    add_market(event, "Odlisna kancelar", "h2h", {"THW Kiel": 1.12, "TSV Hannover": 6.0})
    result = analyzer.analyze([event], cfg)
    check("tip i tak vznikne", result.best is not None)
    if result.best:
        check("pouzity jsou jen kancelare s prevazujici strukturou",
              result.best.n_books == 4, f"(={result.best.n_books})")
        check("trh zustava trojcestny", result.best.n_outcomes == 3)

    # Obracene: prevazuje dvoucestna struktura.
    two_way = two_way_event("mix-2")
    add_market(two_way, "Trojcestna", "h2h",
               {"Alcaraz": 1.20, "Qualifier": 6.0, "Draw": 15.0})
    mixed = analyzer.analyze([two_way], cfg)
    check("prevazujici dvoucestna struktura vyhrava",
          mixed.best is not None and mixed.best.n_outcomes == 2,
          f"({mixed.best.n_outcomes if mixed.best else None})")
    check("odlisna kancelar se nezapocita",
          mixed.best is not None and mixed.best.n_books == 4,
          f"({mixed.best.n_books if mixed.best else None})")


def test_soccer_tip() -> None:
    print("\n[6] Fotbal (volitelny rezim)")
    cfg = make_cfg(enable_soccer=True)
    result = analyzer.analyze([soccer_event()], cfg)
    check("fotbalovy tip vznikl", result.best is not None,
          f"({[r.reason for r in result.rejections]})")
    if result.best:
        check("vybrana je varianta s vyssi sanci",
              result.best.market == "double_chance", f"({result.best.market})")
        check("1X ma sanci nad 88 %", result.best.prob > 0.88,
              f"(={result.best.prob:.4f})")
        check("popis trhu je srozumitelny", "1X" in result.best.market_label)

    off = analyzer.analyze([soccer_event()], make_cfg(enable_soccer=False))
    check("s vypnutym fotbalem se nic nenabizi", not off.tips)

    away_fav = make_event("soc-2", "soccer_epl", "Soccer", "Luton", "Arsenal")
    for name in "ABCD":
        add_market(away_fav, name, "h2h",
                   {"Luton": 9.5, "Draw": 5.25, "Arsenal": 1.36})
        add_market(away_fav, name, "double_chance",
                   {"Luton/Draw": 3.46, "Luton/Arsenal": 1.17, "Draw/Arsenal": 1.16})
    check("favorit venku se vyrazuje",
          not analyzer.analyze([away_fav], cfg).tips)


def test_tip_log() -> None:
    print("\n[7] Denik tipu")
    cfg = make_cfg()
    tlog = TipLog(path=cfg.state_file)
    now = datetime.now(timezone.utc)

    tlog.add([sent_tip("a"), sent_tip("b", pick="Sinner")], "2026-09-10")
    check("tipy se ulozily", len(tlog.tips) == 2)
    check("den odeslani se zapsal", tlog.already_sent_today("2026-09-10"))
    check("jiny den neni oznacen", not tlog.already_sent_today("2026-09-11"))

    tlog.add([sent_tip("a")], "2026-09-10")
    check("stejny tip se neprida dvakrat", len(tlog.tips) == 2)

    check("skoncene zapasy cekaji na vysledek", len(tlog.pending(now)) == 2)

    fresh = TipLog(path=cfg.state_file)
    fresh.add([sent_tip("c", hours_ago=1.0)], "2026-09-10")
    check("jeste probihajici zapas se nedohledava", not fresh.pending(now))

    tlog.record_result(tlog.tips[0], STATUS_WON, "2:0")
    tlog.record_result(tlog.tips[1], STATUS_LOST, "0:2")
    wins, decided = tlog.record()
    check("uspesnost se pocita", (wins, decided) == (1, 2), f"({wins}/{decided})")
    check("vyhodnocene tipy jsou v rekapitulaci",
          len(tlog.recently_settled()) == 2)

    silent = sent_tip("s")
    silent.shown = False
    tlog.add([silent], "2026-09-10")
    tlog.record_result(silent, STATUS_WON, "2:0")
    check("tichy zbytek neni v rekapitulaci", len(tlog.recently_settled()) == 2)
    check("tichy zbytek se nepocita do zobrazene uspesnosti", tlog.record() == (1, 2))

    path = Path("tips.selftest.json")
    ancient = sent_tip("ancient", hours_ago=24 * 200)
    ancient.status = STATUS_WON
    keep = TipLog(path=path, tips=[ancient])
    keep.save()
    check("stare zaznamy se pri ukladani nemazou",
          len(TipLog.load(path).tips) == 1)
    path.unlink(missing_ok=True)

    old = TipLog(path=cfg.state_file)
    old.add([sent_tip("d", hours_ago=100.0)], "2026-09-05")
    check("prilis stary tip se nedohledava", not old.pending(now))
    check("a oznaci se jako neplatny", old.expire_old(now) == 1)
    check("void se nepocita do uspesnosti", old.record() == (0, 0))

    import main as agent_main
    lonely = TipLog(path=cfg.state_file)
    lonely.add([sent_tip("k", hours_ago=100.0)], "2026-09-05")
    agent_main.settle_results(None, lonely)   # nic dalsiho neceka -> API se nevola
    check("stary tip se uzavre, i kdyz nic jineho neceka na vysledek",
          lonely.tips[0].status == STATUS_VOID)


def test_evaluate_score() -> None:
    print("\n[8] Vyhodnoceni vysledku")

    def score(a: str, b: str, home="Alcaraz", away="Qualifier") -> dict:
        return {"completed": True, "scores": [
            {"name": home, "score": a}, {"name": away, "score": b}]}

    won = evaluate_score(sent_tip(), score("2", "0"))
    check("vyhra favorita", won is not None and won[0] == STATUS_WON)
    check("popis obsahuje skore", won is not None and "2:0" in won[1])
    lost = evaluate_score(sent_tip(), score("1", "2"))
    check("prohra favorita", lost is not None and lost[0] == STATUS_LOST)
    check("nedohrany zapas = vklad zpet",
          evaluate_score(sent_tip(), score("1", "1"))[0] == STATUS_VOID)
    check("neukonceny zapas se nevyhodnocuje",
          evaluate_score(sent_tip(), {"completed": False}) is None)
    check("chybejici skore se nevyhodnocuje",
          evaluate_score(sent_tip(), {"completed": True, "scores": []}) is None)

    dc = sent_tip(market="double_chance", home="Arsenal", away="Luton", pick="Arsenal")
    check("1X pri remize vyhrava",
          evaluate_score(dc, score("1", "1", "Arsenal", "Luton"))[0] == STATUS_WON)
    check("1X pri prohre domacich prohrava",
          evaluate_score(dc, score("0", "2", "Arsenal", "Luton"))[0] == STATUS_LOST)

    dnb = sent_tip(market="draw_no_bet", home="Arsenal", away="Luton", pick="Arsenal")
    check("DNB pri remize vraci vklad",
          evaluate_score(dnb, score("1", "1", "Arsenal", "Luton"))[0] == STATUS_VOID)

    handball = sent_tip(home="THW Kiel", away="Hannover", pick="THW Kiel")
    handball.n_outcomes = 3
    check("hazena: remiza je prohrany tip, ne vraceny vklad",
          evaluate_score(handball, score("30", "30", "THW Kiel", "Hannover"))[0]
          == STATUS_LOST)
    legacy = sent_tip(home="THW Kiel", away="Hannover", pick="THW Kiel")
    legacy.sport_key = "handball_germany_bundesliga"
    check("i u starsiho zaznamu hazeny bez typu trhu",
          evaluate_score(legacy, score("28", "28", "THW Kiel", "Hannover"))[0]
          == STATUS_LOST)


def test_sport_selection() -> None:
    print("\n[9] Vyber souteci a spotreba kreditu")
    sports = [
        {"key": "tennis_atp_us_open", "group": "Tennis", "active": True,
         "has_outrights": False},
        {"key": "tennis_atp_challenger", "group": "Tennis", "active": True,
         "has_outrights": False},
        {"key": "basketball_nba", "group": "Basketball", "active": True,
         "has_outrights": False},
        {"key": "baseball_mlb", "group": "Baseball", "active": True,
         "has_outrights": False},
        {"key": "handball_germany_bundesliga", "group": "Handball",
         "active": True, "has_outrights": False},
        {"key": "soccer_epl", "group": "Soccer", "active": True,
         "has_outrights": False},
        {"key": "tennis_wta_china_open", "group": "Tennis", "active": True,
         "has_outrights": False},
        {"key": "americanfootball_ncaaf", "group": "American Football",
         "active": True, "has_outrights": False},
        {"key": "tennis_winner_2026", "group": "Tennis", "active": True,
         "has_outrights": True},
    ]

    def keys_for(cfg: Config) -> list[str]:
        client = OddsAPIClient(cfg)
        client._sports_cache = sports
        return client.select_sport_keys()

    keys = keys_for(make_cfg())
    check("vychozi profil core vezme tenis, basket i hazenou",
          {"tennis_atp_us_open", "basketball_nba",
           "handball_germany_bundesliga"} <= set(keys), f"({keys})")
    check("core nebere baseball", "baseball_mlb" not in keys, f"({keys})")
    check("profil wide baseball uz bere",
          "baseball_mlb" in keys_for(make_cfg(
              sport_profile="wide",
              sport_groups=["Tennis", "Basketball", "Baseball",
                            "Ice Hockey", "Handball"])))
    check("fotbal se do zakladniho vyberu nedostane", "soccer_epl" not in keys)
    check("outright trhy se preskoci", "tennis_winner_2026" not in keys)
    check("turnaj nejvyssi urovne je prvni", keys[0] == "tennis_atp_us_open")

    check("profil core_us bere americky fotbal",
          "americanfootball_ncaaf" in keys_for(make_cfg(
              sport_profile="core_us",
              sport_groups=["Tennis", "Basketball", "Handball",
                            "American Football"])))
    check("core bez americkeho fotbalu", "americanfootball_ncaaf" not in keys)

    # SPORT_KEY_BLACKLIST: presny klic i vzor s hvezdickou
    wta_out = keys_for(make_cfg(sport_key_blacklist=["tennis_wta_*"]))
    check("vzor tennis_wta_* vyradi zensky tenis",
          "tennis_wta_china_open" not in wta_out
          and "tennis_atp_us_open" in wta_out, f"({wta_out})")
    check("vzor nevyradi muzsky tenis ani ostatni sporty",
          {"basketball_nba", "handball_germany_bundesliga"} <= set(wta_out))
    check("presny klic v blacklistu pořád funguje",
          "basketball_nba" not in keys_for(make_cfg(
              sport_key_blacklist=["basketball_nba"])))
    check("blacklist plati i pro whitelist",
          keys_for(make_cfg(sport_key_whitelist=["tennis_wta_china_open",
                                                 "tennis_atp_us_open"],
                            sport_key_blacklist=["tennis_wta_*"]))
          == ["tennis_atp_us_open"])

    from ai_ranker import SPORT_CS
    from notifier import SPORT_ICONS
    check("americky fotbal ma ikonu i cesky nazev",
          SPORT_ICONS.get("American Football") == "🏈"
          and SPORT_CS.get("American Football") == "americky fotbal")

    check("MAX_SPORT_KEYS omezi pocet dotazu",
          len(keys_for(make_cfg(max_sport_keys=2))) == 2)
    check("profil individual vezme jen tenis",
          all(k.startswith("tennis") for k in
              keys_for(make_cfg(sport_profile="individual", sport_groups=["Tennis"]))))
    check("TENNIS_TOP_TIER_ONLY vyradi challengery",
          "tennis_atp_challenger" not in keys_for(make_cfg(tennis_top_tier_only=True)))
    check("whitelist vezme jen existujici souteze",
          keys_for(make_cfg(sport_key_whitelist=["basketball_nba", "neexistuje"]))
          == ["basketball_nba"])
    check("blacklist soutez vyradi",
          "basketball_nba" not in
          keys_for(make_cfg(sport_key_blacklist=["basketball_nba"])))

    budget = make_cfg(max_sport_keys=12).describe_budget()
    check("rozpocet zminuje strop i to, ze necinne souteze jsou zdarma",
          "18 kreditu/den" in budget and "zdarma" in budget, f"({budget})")


def test_config_validation() -> None:
    print("\n[10] Validace konfigurace")

    def fails(**kwargs) -> str:
        try:
            make_cfg(**kwargs).validate(require_telegram=False)
        except ConfigError as exc:
            return str(exc)
        return ""

    check("vychozi nastaveni je validni", fails() == "", f"({fails()})")
    check("chybejici klic se odchyti", "ODDS_API_KEY" in fails(odds_api_key=""))
    check("TIPS_PER_BAND pod 1 se odchyti", "TIPS_PER_BAND" in fails(tips_per_band=0))
    check("MIN_TIP_PROB mimo rozsah se odchyti",
          "MIN_TIP_PROB" in fails(min_tip_prob=1.5))
    check("prekryvajici se pasma se odchyti",
          "ODDS_BANDS" in fails(odds_bands=((1.12, 1.30), (1.25, 1.35))))
    check("pasmo s kurzem pod 1,0 se odchyti",
          "ODDS_BANDS" in fails(odds_bands=((0.9, 1.2),)))
    check("prazdna pasma se odchyti", "ODDS_BANDS" in fails(odds_bands=()))
    check("nekladna virtualni sazka se odchyti",
          "VIRTUAL_STAKE" in fails(virtual_stake=0))
    check("neznama de-vig metoda se odchyti",
          "DEVIG_METHOD" in fails(devig_method="magie"))
    check("zadny sport se odchyti",
          "sport" in fails(sport_groups=[], enable_soccer=False))
    check("chybejici Telegram se odchyti pri odesilani", bool(_telegram_error()))

    check("pasmo bez horni hranice smi byt jen posledni",
          "ODDS_BANDS" in fails(odds_bands=((1.25, math.inf), (1.40, 1.50))))

    cfg = load_config()
    check("load_config projde", cfg.tips_per_band >= 1)
    check("vychozi pasma jsou 1,15-1,19 / 1,20-1,24 / 1,25-1,29",
          make_cfg().odds_bands == ((1.15, 1.19), (1.20, 1.24), (1.25, 1.29)))

    import os
    import config as config_module
    saved = os.environ.get("ODDS_BANDS")
    try:
        os.environ["ODDS_BANDS"] = "1.15-1.19, 1.20-1.24, 1.25+"
        check("zapis 1.25+ se nacte jako pasmo bez horni hranice",
              config_module._odds_bands()
              == ((1.15, 1.19), (1.20, 1.24), (1.25, math.inf)))
        os.environ["ODDS_BANDS"] = "1.15-nesmysl"
        try:
            config_module._odds_bands()
            check("nesmyslny zapis pasma se odchyti", False)
        except ConfigError:
            check("nesmyslny zapis pasma se odchyti", True)
    finally:
        if saved is None:
            os.environ.pop("ODDS_BANDS", None)
        else:
            os.environ["ODDS_BANDS"] = saved


def _telegram_error() -> str:
    try:
        make_cfg(telegram_chat_id="").validate(require_telegram=True)
    except ConfigError as exc:
        return str(exc)
    return ""


def test_message() -> None:
    print("\n[11] Telegramova zprava")
    cfg = make_cfg()
    events = [
        two_way_event("a", "Carlos Alcaraz", "Kvalifikant",
                      prices=(1.14, 1.15, 1.14, 1.16), dog_price=6.5),
        two_way_event("b", "Boston Celtics", "Washington Wizards",
                      prices=(1.20, 1.21, 1.20, 1.24), dog_price=4.8,
                      sport_key="basketball_nba", group="Basketball", hours=14.0),
        three_way_event("c", "SC Magdeburg", "HSG Wetzlar",
                        fav_prices=(1.27, 1.28, 1.27, 1.29), dog_price=6.0,
                        draw_price=12.0),
        two_way_event("d", "Jisty Favorit", "Nekdo",
                      prices=(1.03,) * 4, dog_price=15.0),
    ]
    result = analyzer.analyze(events, cfg)
    selected = analyzer.select_by_band(result.tips, cfg)
    check("Alcaraz (bezny kurz 1,15) je v pasmu A",
          [t.selection for t in selected["A"]] == ["Carlos Alcaraz"],
          f"({[t.selection for t in selected['A']]})")
    check("Celtics (bezny kurz 1,21) jsou v pasmu B",
          [t.selection for t in selected["B"]] == ["Boston Celtics"],
          f"({[t.selection for t in selected['B']]})")
    check("Magdeburg (bezny kurz 1,28) je v pasmu C",
          [t.selection for t in selected["C"]] == ["SC Magdeburg"],
          f"({[t.selection for t in selected['C']]})")
    check("kurz 1,03 je v tichem zbytku", len(selected["pod"]) == 1)

    settled = [sent_tip("x", pick="Sinner"), sent_tip("y", pick="Djokovic")]
    settled[0].status, settled[0].band = STATUS_WON, "A"
    settled[1].status, settled[1].band = STATUS_LOST, "C"
    for tip in settled:
        tip.settled_at = datetime.now(timezone.utc).isoformat()

    from state import BandStats
    stats = {"A": BandStats(wins=12, decided=14, expected_sum=11.8, profit=85.0),
             "B": BandStats(pending=3)}
    msg = format_tips(result, selected, stats, settled, cfg)
    print("\n----- nahled zpravy -----")
    print(msg)
    print("----- konec nahledu -----")

    check("hlavicka je tam", "TIPY NA" in msg)
    check("vsechna tri pasma jsou ve zprave",
          all(f"PASMO {k}" in msg for k in "ABC"))
    check("pasma ukazuji rozsah kurzu",
          "1,15–1,19" in msg and "1,20–1,24" in msg and "1,25–1,29" in msg)
    check("tipy jsou v prislusnych pasmech",
          msg.index("Alcaraz") < msg.index("PASMO B") < msg.index("Celtics")
          < msg.index("PASMO C") < msg.index("Magdeburg"))
    check("tichy zbytek se neposila", "Jisty Favorit" not in msg)
    check("rekapitulace po pasmech", "Minule tipy" in msg and "Sinner" in msg)
    check("statistika pasma", "12/14" in msg and "virtualne" in msg)
    check("ocekavana uspesnost", "cekano" in msg)
    check("pasmo bez vysledku to rekne", "zatim bez vyhodnocenych" in msg)
    check("u hazeny je upozorneni na remizu", "remiza \\= prohra" in msg)
    check("uvadi soupere", "proti" in msg)
    check("zadne varovani o hranici jistoty", "⚠️" not in msg)
    check("neobsahuje edge ani safety index",
          not any(w in msg.lower() for w in ("edge", "safety", "kelly")))
    check("vejde se do limitu Telegramu", len(msg) <= 4096, f"(={len(msg)})")
    check("zadna neescapovana tecka", ". " not in msg.replace("\\. ", " "),
          "(neescapovana tecka = HTTP 400 z Telegramu)")

    empty = analyzer.AnalysisResult(tips=[], considered=7, rejections=[],
                                    funnel={analyzer.STAGE_MARKET: 7})
    none_msg = format_tips(empty, {}, {}, [], cfg)
    check("prazdny den: kazde pasmo rekne dnes nic",
          none_msg.count("dnes nic v tomto pasmu") == 3)
    check("a uvadi duvody", "chybi trh" in none_msg)

    warn = format_quota_warning(12, cfg)
    check("varovani o kvote se sestavi", "kvota" in warn.lower())


# --------------------------------------------------------------------------- #
class FakeAI:
    """Falesny klient Anthropic API -- vraci pripravene odpovedi postupne."""

    def __init__(self, *payloads, text=None, stop_reason="end_turn",
                 error=None, usage=(3000, 1500)):
        self.payloads, self.text = list(payloads), text
        self.stop_reason, self.error, self.usage = stop_reason, error, usage
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        # kopie zprav: agent seznam po volani dal plni
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if self.error:
            raise self.error
        if self.text is not None:
            body = self.text
        else:
            index = min(len(self.calls), len(self.payloads)) - 1
            body = json.dumps(self.payloads[index])
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=body)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=self.usage[0],
                                  output_tokens=self.usage[1],
                                  cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
        )


class FakeGPT:
    """Falesny GPTClient: resers (search=True) a odpovedi v debate postupne."""

    def __init__(self, *payloads, research="- fakt z webu (zdroj)",
                 research_error=None, error=None, text=None, no_search=0,
                 never_search=None):
        self.payloads, self.research = list(payloads), research
        self.no_search = no_search          # kolik reserse probehne bez hledani
        self.never_search = never_search    # u zapasu s timto textem nehleda nikdy
        self.research_error, self.error, self.text = research_error, error, text
        self.calls: list[dict] = []
        self.debate_calls = 0
        self.model = "gpt-test"

    def generate(self, messages, *, system, search=False, schema=None, model=None,
                 **kwargs):
        from gpt import GPTReply
        self.calls.append({"messages": list(messages), "system": system,
                           "search": search, "schema": schema, "model": model, **kwargs})
        if search:
            if self.research_error:
                raise self.research_error
            prompt_text = messages[0]["content"]
            if self.never_search and self.never_search in prompt_text:
                return GPTReply(text="z pameti: tym neexistuje")
            if self.no_search > 0:
                self.no_search -= 1
                return GPTReply(text="z pameti: tym neexistuje")
            return GPTReply(text=self.research, sources=[("web", "https://x")],
                            queries=["q"], searches=1, input_tokens=100, output_tokens=200)
        if self.error:
            raise self.error
        self.debate_calls += 1
        if self.text is not None:
            return GPTReply(text=self.text)
        index = min(self.debate_calls, len(self.payloads)) - 1
        return GPTReply(text=json.dumps(self.payloads[index]),
                        input_tokens=1000, output_tokens=500)


def verdict(scores: dict[str, dict[str, int]], reply="", evidence="solid") -> dict:
    """Odpoved agenta: {pasmo: {id: odhad}}."""
    return {"bands": [
        {"band": band, "reply_to_opponent": reply, "candidates": [
            {"id": cid, "safety": safety, "argument": f"arg {cid}", "evidence": evidence}
            for cid, safety in ids.items()]}
        for band, ids in scores.items()]}


def ai_pool(cfg):
    """5 kandidatu v pasmu A, 2 v B a 1 (hazena) v C -- vsichni prochazi kontrolami."""
    events = [two_way_event(f"a{i}", f"FavA{i}", f"DogA{i}", prices=(1.16,) * 4,
                            dog_price=4.4 + 0.3 * i) for i in range(5)]
    events += [two_way_event(f"b{i}", f"FavB{i}", f"DogB{i}", prices=(1.22,) * 4,
                             dog_price=4.0 + 0.4 * i) for i in range(2)]
    events.append(three_way_event("c0", "FavC0", "DogC0", fav_prices=(1.28,) * 4,
                                  dog_price=4.0, draw_price=9.0))
    result = analyzer.analyze(events, cfg)
    wide = analyzer.select_by_band(result.tips, cfg, limit=cfg.ai_pool_per_band)
    return result, {k: wide[k] for k in "ABC"}


# odhady, na kterych se agenti hned shodnou (1. misto A1, B2)
AGREE_A = {"A1": 90, "A2": 85, "A3": 80, "A4": 70, "A5": 60}
AGREE_B = {"B1": 70, "B2": 80}
AGREE_C = {"C1": 75}


def ai_cfg(**overrides):
    base = dict(anthropic_api_key="test-key", openai_api_key="o-key", ai_ranking=True)
    base.update(overrides)
    return make_cfg(**base)


def test_ai_ranking() -> None:
    print("\n[13] AI debata (GPT x Claude)")
    import ai_debate
    import ai_ranker
    cfg = ai_cfg()
    now = datetime.now(timezone.utc)
    result, pool = ai_pool(cfg)

    check("pool: pasmo A ma 5 kandidatu, B 2, C 1",
          [len(pool[k]) for k in "ABC"] == [5, 2, 1],
          f"({[len(pool[k]) for k in 'ABC']})")
    check("odds_rank je 1..5 podle sance",
          [t.odds_rank for t in pool["A"]] == [1, 2, 3, 4, 5])
    check("kandidati jsou serazeni od nejvyssi sance",
          all(pool["A"][i].prob >= pool["A"][i + 1].prob for i in range(4)))
    narrow = analyzer.select_by_band(result.tips, cfg)
    check("bez limitu zustava TIPS_PER_BAND", len(narrow["A"]) == 3)
    check("odds_rank dostavaji i tipy mimo vyber",
          sum(1 for t in result.tips if t.odds_rank is not None) == len(result.tips))
    ids = ai_ranker.candidate_ids(pool)
    check("id kandidatu",
          set(ids) == {"A1", "A2", "A3", "A4", "A5", "B1", "B2", "C1"},
          f"({sorted(ids)})")

    # --- hned shoda ------------------------------------------------------------ #
    claude = FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}))
    gem = FakeGPT(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}))
    ranking = ai_debate.run_debate(pool, cfg, now, claude_client=claude, gpt_client=gem)
    check("debata vratila poradi", ranking is not None)
    if ranking is None:
        return
    research = [c for c in gem.calls if c["search"]]
    check("resers pro kazdy zapas zvlast (5 + 2 + 1)", len(research) == 8)
    research_text = research[0]["messages"][0]["content"]
    check("resers hleda vse podstatne",
          all(k in research_text.lower()
              for k in ("injuries", "head-to-head", "weather", "rest", "style",
                        "previews", "risks")), f"({research_text[:200]})")
    check("resers zna favorita a sanci trhu",
          any(t.pick_team in c["messages"][0]["content"]
              for c in research for t in pool["A"]))
    check("pri shode v uvodu zadne dalsi kolo",
          len(claude.calls) == 1 and gem.debate_calls == 1)
    check("shoda ve vsech pasmech vcetne jednoho kandidata",
          ranking.consensus == {"A": True, "B": True, "C": True}, f"({ranking.consensus})")
    check("poradi A podle prumeru", ranking.order["A"] == ["A1", "A2", "A3", "A4", "A5"])
    check("B: agenti povysili B2", ranking.order["B"] == ["B2", "B1"])
    check("pasmo s 1 kandidatem dostane posudek obou agentu",
          ranking.order["C"] == ["C1"]
          and ranking.verdicts["C1"].reason.startswith("GPT 75 % · Claude 75 %")
          and ranking.verdicts["C1"].confidence == "agreed")

    call = claude.calls[0]
    prompt = call["messages"][0]["content"][0]["text"]
    check("uvodni zadani obsahuje vsechny kandidaty",
          all(cid in prompt for cid in ("A1", "A5", "B1", "B2", "C1")))
    check("uvodni zadani obsahuje resers", "fakt z webu" in prompt)
    check("tenis: domaci/hoste nema vyznam", "home/away is meaningless" in prompt)
    check("zadani uvadi sanci trhu", "win probability" in prompt)
    check("Claude je skeptik, GPT analytik",
          "SKEPTIC" in call["system"] and "ANALYST" in gem.calls[-1]["system"])
    check("pravidla zakazuji vymyslet fakta",
          "Never invent" in call["system"] and "Never invent" in gem.calls[-1]["system"])
    check("Claude: model a effort z nastaveni",
          call["model"] == "claude-sonnet-5" and call["output_config"]["effort"] == "medium")
    check("Claude: vystup vazany na JSON schema",
          call["output_config"]["format"]["type"] == "json_schema")
    check("Claude: zadne parametry, ktere Sonnet 5 odmita",
          not any(k in call for k in ("temperature", "top_p", "top_k")))
    check("Claude: uvodni zadani jde do prompt cache",
          call["messages"][0]["content"][0].get("cache_control") == {"type": "ephemeral"})
    check("GPT: odpoved vazana na schema, resers bez schematu",
          gem.calls[-1]["schema"] is not None and research[0]["schema"] is None)
    check("verdikt nese oba odhady",
          ranking.verdicts["A1"].reason.startswith("GPT 90 % · Claude 90 %"),
          f"({ranking.verdicts['A1'].reason})")
    check("1. misto pri shode = 'agreed'", ranking.verdicts["A1"].confidence == "agreed")
    check("ostatni podle dat", ranking.verdicts["A2"].confidence == "solid")
    check("cena Clauda se spocita", ranking.cost_usd is not None and ranking.cost_usd > 0)
    check("prubeh debaty se zapise",
          "# Debata" in ranking.transcript and "shoda na A1 hned" in ranking.transcript
          and "fakt z webu" in ranking.transcript)

    # --- neshoda, pak GPT ustoupi --------------------------------------------- #
    _, pool2 = ai_pool(cfg)
    c_open = {"A": {"A1": 70, "A2": 75, "A3": 88, "A4": 60, "A5": 50}, "B": AGREE_B, "C": AGREE_C}
    g_open = {"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}
    g_turn = {"A": {"A1": 80, "A2": 78, "A3": 86, "A4": 65, "A5": 55}}
    claude2 = FakeAI(verdict(c_open))
    gem2 = FakeGPT(verdict(g_open), verdict(g_turn, reply="Uznavam, A3 je lepsi."))
    r2 = ai_debate.run_debate(pool2, cfg, now, claude_client=claude2, gpt_client=gem2)
    check("neshoda -> debata -> shoda", r2 is not None and r2.consensus.get("A") is True)
    if r2:
        check("vitez debaty je na 1. miste", r2.order["A"][0] == "A3", f"({r2.order['A']})")
        check("Claude uz nemusel odpovidat", len(claude2.calls) == 1)
        turn_text = gem2.calls[-1]["messages"][-1]["content"]
        check("odpoved se tyka jen sporneho pasma",
              "band(s) A" in turn_text and "BAND B" not in turn_text)
        check("agent vidi argumenty soupere", "arg A3" in turn_text)
        check("zadani varuje pred ustupem kvuli dohode", "Giving in" in turn_text)
        check("GPT vidi celou svou konverzaci", len(gem2.calls[-1]["messages"]) == 3)
        check("prubeh zaznamena odpoved", "Uznavam, A3 je lepsi." in r2.transcript
              and "po 1. odpovedi" in r2.transcript)

    # --- stejne 1. misto, ale odhady daleko od sebe ---------------------------- #
    _, pool_gap = ai_pool(cfg)
    c_low = {"A": AGREE_A, "B": AGREE_B, "C": {"C1": 55}}
    claude_gap = FakeAI(verdict(c_low))
    gem_gap = FakeGPT(verdict({"A": AGREE_A, "B": AGREE_B, "C": {"C1": 80}}),
                         verdict({"C": {"C1": 62}}, reply="Uznavam riziko unavy."))
    r_gap = ai_debate.run_debate(pool_gap, cfg, now, claude_client=claude_gap,
                                 gpt_client=gem_gap)
    check("i jediny kandidat: rozdil odhadu 25 bodu -> debata",
          r_gap is not None and gem_gap.debate_calls == 2)
    if r_gap:
        turn_text = gem_gap.calls[-1]["messages"][-1]["content"]
        check("odpoved se tyka jen pasma C", "band(s) C" in turn_text
              and "BAND A" not in turn_text)
        check("zadani vysvetluje, ze jde o rozdil v odhadu",
              "differ by more than 10 points" in turn_text)
        check("po priblizeni odhadu (62 vs 55) je shoda",
              r_gap.consensus["C"] is True and "po 1. odpovedi" in r_gap.transcript)
        check("agenti se ve zbylych pasmech shodli hned", r_gap.consensus["A"] is True)
    check("shoda vyzaduje stejne 1. misto i blizke odhady",
          ai_debate.agreed(ai_debate.BandVerdict({"A1": ai_debate.Rating(80)}),
                           ai_debate.BandVerdict({"A1": ai_debate.Rating(70)}), 10)
          and not ai_debate.agreed(ai_debate.BandVerdict({"A1": ai_debate.Rating(80)}),
                                   ai_debate.BandVerdict({"A1": ai_debate.Rating(69)}), 10))

    # --- neshoda az do konce -------------------------------------------------- #
    _, pool3 = ai_pool(cfg)
    c_stub = {"A": {"A1": 70, "A2": 90, "A3": 60, "A4": 50, "A5": 40}, "B": AGREE_B, "C": AGREE_C}
    g_stub = {"A": {"A1": 95, "A2": 80, "A3": 60, "A4": 50, "A5": 40}, "B": AGREE_B, "C": AGREE_C}
    claude3 = FakeAI(verdict(c_stub), verdict({"A": c_stub["A"]}))
    gem3 = FakeGPT(verdict(g_stub), verdict({"A": g_stub["A"]}))
    r3 = ai_debate.run_debate(pool3, ai_cfg(ai_max_turns=3), now,
                              claude_client=claude3, gpt_client=gem3)
    check("bez shody se debata ukonci po AI_MAX_TURNS", r3 is not None
          and gem3.debate_calls == 3 and len(claude3.calls) == 2,
          f"(G {gem3.debate_calls}, C {len(claude3.calls)})")
    if r3:
        check("bez shody rozhodne prumer (A2: 85 > A1: 82,5)",
              r3.order["A"][0] == "A2" and r3.consensus["A"] is False)
        check("1. misto bez shody = 'disputed'", r3.verdicts["A2"].confidence == "disputed")
        last_text = gem3.calls[-1]["messages"][-1]["content"]
        check("posledni odpoved vi, ze je posledni", "LAST exchange" in last_text)
        check("prubeh rika, ze se neshodli", "BEZ SHODY" in r3.transcript)
    r0 = ai_debate.run_debate(ai_pool(cfg)[1], ai_cfg(ai_max_turns=0), now,
                              claude_client=FakeAI(verdict(c_stub)),
                              gpt_client=FakeGPT(verdict(g_stub)))
    check("AI_MAX_TURNS=0: jen uvodni verdikty, rozhodne prumer",
          r0 is not None and r0.order["A"][0] == "A2")

    # --- tolerance k chybam v odpovedi ---------------------------------------- #
    band_ids = {"A": ["A1", "A2", "A3"]}
    parsed, notes = ai_debate.parse_verdict({"bands": [{"band": "PASMO A", "candidates": [
        {"id": "a2", "safety": "81", "argument": "  dvojite   mezery ", "evidence": "solid"},
        {"id": "A2", "safety": 99, "argument": "duplicita", "evidence": "solid"},
        {"id": "Z9", "safety": 99, "argument": "neexistuje", "evidence": "solid"},
        {"id": "A1", "safety": 150, "argument": "x", "evidence": "nesmysl"},
        {"id": "A3", "safety": "hodne", "argument": "x", "evidence": "solid"},
    ]}]}, band_ids)
    a = parsed["A"]
    check("id se bere bez ohledu na velikost pismen", a.ratings["A2"].safety == 81)
    check("odhad nad 100 se orizne", a.ratings["A1"].safety == 100)
    check("neplatny odhad = kandidat bez odhadu jde na konec",
          a.ratings["A3"].safety is None and a.order() == ["A1", "A2", "A3"])
    check("mezery se srovnaji", a.ratings["A2"].argument == "dvojite mezery")
    check("neplatne 'evidence' = thin", a.ratings["A1"].evidence == "thin")
    check("problemy odpovedi se hlasi", len(notes) == 2, f"({notes})")
    check("shoda odhadu -> rozhodne trh",
          ai_debate.BandVerdict({"A2": ai_debate.Rating(80), "A1": ai_debate.Rating(80)})
          .pick() == "A1")

    # --- resers selze, debata pokracuje -------------------------------------- #
    from gpt import GPTError
    _, pool4 = ai_pool(cfg)
    r4 = ai_debate.run_debate(
        pool4, cfg, now, claude_client=FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C})),
        gpt_client=FakeGPT(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}),
                                 research_error=GPTError("HTTP 429")))
    check("selhani reserse debatu nezastavi", r4 is not None)
    if r4:
        check("agenti i prubeh vi, ze data chybi", "selhala" in r4.transcript)
        check("prubeh ma resers ke kazdemu zapasu zvlast",
              "### Resers A1" in r4.transcript and "### Resers C1" in r4.transcript)

    # --- GPT nehleda na webu ---------------------------------------------- #
    _, pool_ns = ai_pool(cfg)
    gem_ns = FakeGPT(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}), no_search=1)
    r_ns = ai_debate.run_debate(pool_ns, cfg, now, gpt_client=gem_ns,
                                claude_client=FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C})))
    research_ns = [c for c in gem_ns.calls if c["search"]]
    check("resers: vyhledavani omezene poctem z nastaveni",
          all(c.get("max_searches") == cfg.openai_max_searches for c in research_ns))
    check("resers pouziva vlastni model (OPENAI_RESEARCH_MODEL)",
          all(c["model"] == cfg.openai_research_model for c in research_ns))
    check("bez hledani se resers zopakuje s duraznym pokynem",
          len(research_ns) == 9
          and sum("did not use web search" in c["messages"][0]["content"]
                  for c in research_ns) == 1)
    check("po opakovani se pouzije nalezeny rozbor",
          r_ns is not None and "z pameti" not in r_ns.transcript)
    _, pool_ns2 = ai_pool(cfg)
    r_ns2 = ai_debate.run_debate(
        pool_ns2, cfg, now,
        gpt_client=FakeGPT(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}),
                                 never_search=pool_ns2["A"][0].pick_team),
        claude_client=FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C})))
    check("rozbor z pameti (ani napodruhe nehledal) se zahodi",
          r_ns2 is not None and "z pameti" not in r_ns2.transcript
          and "nic nevyhledal" in r_ns2.transcript)
    check("resers nikdy netvrdi, ze tym neexistuje",
          "never claim a team or player does not exist" in ai_debate.RESEARCH_SYSTEM)

    # --- selhani agentu vede zpet ke kurzum ---------------------------------- #
    def falls_back(name, claude_client, gpt_client, use_cfg=None):
        _, pl = ai_pool(cfg)
        res = ai_debate.run_debate(pl, use_cfg or cfg, now, claude_client=claude_client,
                                   gpt_client=gpt_client)
        untouched = all(t.ai_rank is None for tips in pl.values() for t in tips)
        check(name, res is None and untouched)

    ok_g = lambda: FakeGPT(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}))   # noqa: E731
    ok_c = lambda: FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}))       # noqa: E731
    falls_back("chyba Clauda -> kurzy", FakeAI(error=RuntimeError("boom")), ok_g())
    falls_back("chyba GPT v debate -> kurzy", ok_c(),
               FakeGPT(error=GPTError("HTTP 500")))
    falls_back("neplatny JSON od Clauda -> kurzy", FakeAI(text="{ nejsem json"), ok_g())
    falls_back("neplatny JSON od GPT -> kurzy", ok_c(), FakeGPT(text="nic"))
    falls_back("Claude: dosly tokeny -> kurzy",
               FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}), stop_reason="max_tokens"), ok_g())
    falls_back("Claude: odmitnuti -> kurzy",
               FakeAI(verdict({"A": AGREE_A, "B": AGREE_B, "C": AGREE_C}), stop_reason="refusal"), ok_g())
    falls_back("uvodni verdikt bez pasma -> kurzy", FakeAI(verdict({"A": AGREE_A})), ok_g())
    falls_back("odpoved bez pole bands -> kurzy", FakeAI({"neco": 1}), ok_g())
    falls_back("AI_RANKING=false -> kurzy", ok_c(), ok_g(), use_cfg=ai_cfg(ai_ranking=False))

    idle_c, idle_g = ok_c(), ok_g()
    _, pool_solo = ai_pool(cfg)
    empty = {"A": [], "B": [], "C": []}
    check("kdyz v pasmech nic neni, API se nevola",
          ai_debate.run_debate(empty, cfg, now, claude_client=idle_c, gpt_client=idle_g)
          is None and not idle_c.calls and not idle_g.calls)
    check("bez klice GPT se nic nevola",
          ai_debate.skip_reason(pool_solo, ai_cfg(openai_api_key="")) == "chybi OPENAI_API_KEY")
    check("bez klice Anthropic se nic nevola",
          ai_debate.skip_reason(pool_solo, ai_cfg(anthropic_api_key=""))
          == "chybi ANTHROPIC_API_KEY")
    check("bez klicu se vubec nic nespusti",
          ai_debate.run_debate(pool_solo, make_cfg(), now) is None)

    # --- cena ---------------------------------------------------------------- #
    check("cena: 3000 vstup + 1500 vystup pri 2/10 USD",
          math.isclose(ai_ranker.estimate_cost("claude-sonnet-5", 3000, 1500), 0.021))
    check("cena: cteni z cache je za desetinu",
          math.isclose(ai_ranker.estimate_cost("claude-sonnet-5", 0, 0, cache_read=10_000),
                       0.002))
    check("neznamy model nema odhad ceny",
          ai_ranker.estimate_cost("neznamy-model", 1000, 1000) is None)

    # --- choose_tips (cely vyber) ------------------------------------------ #
    import main as agent_main
    pick_a5 = {"A": {"A1": 70, "A2": 69, "A3": 68, "A4": 60, "A5": 90}, "B": AGREE_B, "C": AGREE_C}
    result5, _ = ai_pool(cfg)
    selected, extras5, ai5, failed5 = agent_main.choose_tips(
        result5.tips, cfg, now, ai_client=FakeAI(verdict(pick_a5)),
        gpt_client=FakeGPT(verdict(pick_a5)))
    check("choose_tips: poradi z debaty se pouzije",
          ai5 is not None and selected["A"][0].odds_rank == 5 and not failed5)
    check("choose_tips: tichy zbytek se orezava na TIPS_PER_BAND",
          all(len(selected[k]) <= 3 for k in analyzer.REST_KEYS))
    check("choose_tips: vyrazeny kurzovy tip je mezi 'navic'",
          [t.odds_rank for t in extras5["A"]] == [3])
    check("tipy dostaly ai_rank a duvod",
          selected["A"][0].ai_rank == 1 and "arg A5" in selected["A"][0].ai_reason)

    result6, _ = ai_pool(cfg)
    sel6, ext6, ai6, failed6 = agent_main.choose_tips(result6.tips, make_cfg(), now)
    check("bez klicu: cisty kurzovy vyber, zadne 'navic'",
          ai6 is None and ext6 == {} and not failed6
          and [t.odds_rank for t in sel6["A"]] == [1, 2, 3])
    result7, _ = ai_pool(cfg)
    sel7, ext7, ai7, failed7 = agent_main.choose_tips(
        result7.tips, cfg, now, ai_client=FakeAI(error=RuntimeError("boom")),
        gpt_client=ok_g())
    check("pri selhani AI: stejny vyber jako bez AI",
          ai7 is None and ext7 == {}
          and [t.selection for t in sel7["A"]] == [t.selection for t in sel6["A"]])
    check("pri selhani AI se to oznaci (zprava to musi rict)", failed7)

    few = [two_way_event("only1", "SoloFav", "SoloDog", prices=(1.05,) * 4,
                         dog_price=12.0)]          # jen pod pasmy
    res_few = analyzer.analyze(few, cfg)
    _, _, ai_few, failed_few = agent_main.choose_tips(
        res_few.tips, cfg, now, ai_client=ok_c(), gpt_client=ok_g())
    check("kdyz neni co radit, AI se nevola a neni to selhani",
          ai_few is None and not failed_few)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = agent_main.save_transcript("# Debata\n", "2026-09-22", Path(tmp) / "debaty")
        check("prubeh debaty se ulozi do debaty/RRRR-MM-DD.md",
              path is not None and path.name == "2026-09-22.md"
              and path.read_text(encoding="utf-8") == "# Debata\n")


def test_gpt_client() -> None:
    print("\n[13a] Klient OpenAI Responses API")
    from gpt import GPTClient, GPTError, estimate_cost

    class Resp:
        def __init__(self, status, data=None, text=""):
            self.status_code, self._data, self.text = status, data, text

        def json(self):
            return self._data

    class Http:
        def __init__(self, *responses):
            self.responses, self.calls = list(responses), []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    ok = {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "web_search_call", "status": "completed",
         "action": {"type": "search", "query": "Sinner injury"}},
        {"type": "web_search_call", "status": "completed",
         "action": {"type": "open_page", "url": "https://a"}},
        {"type": "message", "content": [{
            "type": "output_text", "text": "odpoved",
            "annotations": [
                {"type": "url_citation", "url": "https://a", "title": "atp.com"},
                {"type": "url_citation", "url": "https://a", "title": "atp.com"}]}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 2000}}
    waits: list[float] = []
    http = Http(Resp(503, text="busy"), Resp(200, ok))
    client = GPTClient("tajny-klic", "gpt-5-mini", http=http, sleep=waits.append)
    reply = client.generate([{"role": "user", "content": "q"}], system="s", search=True,
                            max_searches=3, schema={"type": "object"})
    check("text odpovedi", reply.text == "odpoved")
    check("zdroje a dotazy bez duplicit",
          reply.sources == [("atp.com", "https://a")] and reply.queries == ["Sinner injury"])
    check("placene hledani = jen akce 'search'", reply.searches == 1)
    check("503 -> pocka a zkusi znovu", len(http.calls) == 2 and waits == [10.0])
    url, kwargs = http.calls[-1]
    body = kwargs["json"]
    check("klic je v hlavicce, ne v URL",
          "tajny-klic" not in url and kwargs["headers"]["Authorization"] == "Bearer tajny-klic")
    check("vyhledavani je VYNUCENE", body["tools"] == [{"type": "web_search"}]
          and body["tool_choice"] == "required")
    check("pocet hledani je omezeny", body["max_tool_calls"] == 3)
    check("vystup vazany na JSON schema (strict)",
          body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"])
    check("nic se neuklada u OpenAI", body["store"] is False)
    check("cena: tokeny + hledani",
          math.isclose(client.cost_usd, 1000 * 0.25e-6 + 2000 * 2e-6 + 0.01)
          and client.searches == 1, f"({client.cost_usd})")
    check("neznamy model nema odhad ceny", estimate_cost("neznamy", 1, 1) is None)

    plain_http = Http(Resp(200, ok))
    GPTClient("k", "vychozi", http=plain_http).generate(
        [{"role": "user", "content": "q"}], system="s", model="jiny")
    plain = plain_http.calls[0][1]["json"]
    check("bez hledani zadne nastroje", "tools" not in plain and "tool_choice" not in plain)
    check("model jde zmenit pro jeden dotaz", plain["model"] == "jiny")

    def raises(name, *responses, contains=""):
        http_r = Http(*responses)
        try:
            GPTClient("k", "m", http=http_r, sleep=lambda s: None).generate(
                [{"role": "user", "content": "q"}], system="s")
            check(name, False)
        except GPTError as exc:
            check(name, contains in str(exc), f"({exc})")
        return http_r

    raises("HTTP 400 -> chyba hned", Resp(400, text="bad"))
    no_credit = raises("dosly kredit -> chyba bez zbytecnych pokusu",
                       Resp(429, text='{"error": {"code": "insufficient_quota"}}'),
                       contains="kredit")
    check("dosly kredit -> jen jeden pokus", len(no_credit.calls) == 1)
    raises("opakovane 503 -> chyba", Resp(503), Resp(503), Resp(503))
    raises("nedokoncena odpoved -> chyba",
           Resp(200, {"status": "incomplete",
                      "incomplete_details": {"reason": "max_output_tokens"}}),
           contains="max_output_tokens")
    raises("odmitnuti -> chyba", Resp(200, {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "refusal", "refusal": "ne"}]}]}))
    raises("prazdna odpoved -> chyba", Resp(200, {"status": "completed", "output": []}))


def test_ai_tracking() -> None:
    print("\n[13b] AI: ukladani a porovnani s kurzovym vyberem")
    path = Path("tips.selftest.json")
    tlog = TipLog(path=path)

    def add(eid, status, odds_rank, ai_rank, shown=True, band="A"):
        tip = sent_tip(eid)
        tip.status, tip.band, tip.shown = status, band, shown
        tip.odds_rank, tip.ai_rank = odds_rank, ai_rank
        tip.median_odds = 1.17
        tlog.tips.append(tip)

    add("t1", STATUS_WON, 1, 2)                   # oba vybery, vyhral
    add("t2", STATUS_LOST, 3, 5, shown=False)     # jen kurzy (AI vyradila), prohral
    add("t3", STATUS_WON, 4, 1)                   # jen AI, vyhral
    add("t4", STATUS_WON, 2, 3)                   # oba vybery, vyhral
    add("t5", STATUS_WON, 1, None)                # z doby pred AI -> ignoruje se
    add("t6", STATUS_OPEN, 1, 1)                  # jeste nevyhodnoceny -> ignoruje se
    cmp = tlog.ai_comparison(3)
    check("AI vyber: 3/3", cmp.ai_top == (3, 3), f"({cmp.ai_top})")
    check("kurzovy vyber: 2/3", cmp.odds_top == (2, 3), f"({cmp.odds_top})")
    check("jen AI: 1/1", cmp.ai_only == (1, 1), f"({cmp.ai_only})")
    check("jen kurzy: 0/1", cmp.odds_only == (0, 1), f"({cmp.odds_only})")
    check("porovnani ma data", cmp.has_data)
    check("bez AI dat neni co porovnavat",
          not TipLog(path=path).ai_comparison(3).has_data)

    stats = tlog.band_stats("A", 100.0)
    check("statistika pasma ignoruje tichy zaznam", stats.decided == 4,
          f"({stats.decided})")
    check("statistika vcetne tichych zaznamu je k dispozici",
          tlog.band_stats("A", 100.0, shown_only=False).decided == 5)

    tlog.save()
    loaded = TipLog.load(path)
    t2 = next(t for t in loaded.tips if t.event_id == "t2")
    check("AI pole prezijou ulozeni",
          t2.odds_rank == 3 and t2.ai_rank == 5 and not t2.shown)
    path.write_text(json.dumps({"tips": [{
        "sent_at": "x", "event_id": "old", "sport_key": "k", "sport_title": "t",
        "commence_time": "2026-09-01T10:00:00+00:00", "home_team": "a",
        "away_team": "b", "market": "h2h", "selection": "a", "pick_team": "a",
        "prob": 0.8, "odds": 1.2}]}), encoding="utf-8")
    old = TipLog.load(path).tips[0]
    check("starsi zaznam bez AI poli se nacte",
          old.ai_rank is None and old.odds_rank is None)
    path.unlink(missing_ok=True)


def test_ai_message() -> None:
    print("\n[13c] AI: zprava na Telegram a nastaveni")
    import ai_debate
    cfg = ai_cfg()
    now = datetime.now(timezone.utc)
    result, pool = ai_pool(cfg)
    c_open = {"A": {"A1": 70, "A2": 91, "A3": 60, "A4": 50, "A5": 40},
              "B": {"B1": 80, "B2": 70}, "C": {"C1": 66}}
    g_open = {"A": {"A1": 95, "A2": 80, "A3": 60, "A4": 50, "A5": 40},
              "B": {"B1": 80, "B2": 70}, "C": {"C1": 70}}
    payload_c = verdict(c_open, evidence="thin")
    payload_c["bands"][0]["candidates"][1]["argument"] = "Vyrovnany souboj, " + "dlouhy text " * 40
    ranking = ai_debate.run_debate(
        pool, ai_cfg(ai_max_turns=0), now,
        claude_client=FakeAI(payload_c), gpt_client=FakeGPT(verdict(g_open)))
    import ai_ranker
    chosen, _ = ai_ranker.apply_ai_order(pool, ranking, cfg)

    from state import BandStats
    stats = {"A": BandStats(), "B": BandStats(), "C": BandStats()}
    msg = format_tips(result, chosen, stats, [], cfg, ai_used=True)
    print("\n----- nahled zpravy s AI -----")
    print(msg)
    print("----- konec nahledu -----")
    check("poznamka AI je ve zprave", "arg B1" in msg)
    check("i jediny zapas v pasmu ma poznamku", "GPT 70 % · Claude 66 %" in msg)
    check("odhady obou agentu jsou ve zprave", "GPT 95 % · Claude 70 %" in msg)
    check("shoda ma podani ruky", "🤝" in msg)
    check("neshoda ma varovani", "⚠️" in msg)
    check("malo dat ma otaznik", "❔" in msg)
    check("zprava vysvetluje znacky a rika, odkud poradi je",
          "debata AI" in msg and "GPT" in msg and "Claude" in msg)
    check("dlouha poznamka se zkrati", "dlouhy text " * 20 not in msg and "…" in msg)
    check("vejde se do limitu Telegramu", len(msg) <= 4096, f"(={len(msg)})")
    check("zadna neescapovana tecka", ". " not in msg.replace("\\. ", " "),
          "(neescapovana tecka = HTTP 400 z Telegramu)")

    fresh_result, _ = ai_pool(cfg)      # cerstve tipy, kterych se AI nedotkla
    plain = format_tips(fresh_result,
                        analyzer.select_by_band(fresh_result.tips, cfg),
                        stats, [], cfg)
    check("bez AI zadne upozorneni ani poznamky",
          "debata AI" not in plain and "\U0001f4ac" not in plain
          and "❔" not in plain and "🤝" not in plain)

    failed_msg = format_tips(fresh_result,
                             analyzer.select_by_band(fresh_result.tips, cfg),
                             stats, [], cfg, ai_failed=True)
    check("pri selhani AI zprava na to upozorni",
          "AI debata dnes selhala" in failed_msg and "poradi je podle kurzu" in failed_msg)
    check("bez selhani zadne upozorneni na selhani",
          "dnes selhala" not in plain and "dnes selhala" not in msg)

    def fails(**kwargs) -> str:
        try:
            make_cfg(**kwargs).validate(require_telegram=False)
        except ConfigError as exc:
            return str(exc)
        return ""

    check("AI_POOL_PER_BAND mensi nez TIPS_PER_BAND se odchyti",
          "AI_POOL_PER_BAND" in fails(ai_ranking=True, ai_pool_per_band=2, tips_per_band=3))
    check("neznamy AI_EFFORT se odchyti", "AI_EFFORT" in fails(ai_effort="extreme"))
    check("prazdny AI_MODEL se odchyti", "AI_MODEL" in fails(ai_model=""))
    check("AI_MAX_TURNS mimo rozsah se odchyti", "AI_MAX_TURNS" in fails(ai_max_turns=11))
    check("prazdny OPENAI_MODEL se odchyti", "OPENAI_MODEL" in fails(openai_model=""))
    check("AI_AGREE_GAP mimo rozsah se odchyti", "AI_AGREE_GAP" in fails(ai_agree_gap=-1))
    check("OPENAI_MAX_SEARCHES 0 se odchyti",
          "OPENAI_MAX_SEARCHES" in fails(openai_max_searches=0))
    check("AI je aktivni jen s obema klici",
          ai_cfg().ai_active and not make_cfg().ai_active
          and not ai_cfg(openai_api_key="").ai_active
          and not ai_cfg(anthropic_api_key="").ai_active
          and not ai_cfg(ai_ranking=False).ai_active)


def test_chat_prompt() -> None:
    print("\n[13d] Vypis dalsich kandidatu a text pro chat")
    import main as agent_main
    from notifier import TelegramNotifier, format_chat_prompt
    cfg = make_cfg(ai_ranking=False, list_per_band=4)
    now = datetime.now(timezone.utc)
    result, _ = ai_pool(cfg)
    selected, _, _, _ = agent_main.choose_tips(result.tips, cfg, now)
    more = agent_main.more_candidates(result.tips, selected, cfg)
    check("hlavni tipy zustavaji 3", len(selected["A"]) == 3)
    check("dalsi kandidati doplni pasmo do LIST_PER_BAND",
          len(more["A"]) == 1 and more["A"][0].odds_rank == 4, f"({len(more['A'])})")
    check("dalsi kandidati se neopakuji s hlavnimi tipy",
          not any(m is s for m in more["A"] for s in selected["A"]))
    check("pasmo bez dalsich kandidatu je prazdne", more["B"] == [] and more["C"] == [])

    from state import BandStats
    stats = {"A": BandStats(), "B": BandStats(), "C": BandStats()}
    msg = format_tips(result, selected, stats, [], cfg, more=more)
    check("zprava ukazuje dalsi kandidaty", "dalsi kandidati" in msg and "4\\. " in msg)
    check("zprava s kandidaty se vejde do limitu", len(msg) <= 4096)

    groups = {k: selected[k] + more[k] for k in "ABC"}
    prompt = format_chat_prompt(groups, cfg, now)
    print("\n----- nahled textu pro chat -----")
    print(prompt)
    print("----- konec nahledu -----")
    check("text pro chat obsahuje vsechny zapasy",
          all(f"{k}{i}" in prompt for k in "ABC" for i in range(1, len(groups[k]) + 1)))
    check("text pro chat obsahuje zadani", "vyhledej na webu" in prompt
          and "3 nejjistější" in prompt)
    check("text pro chat uvadi sport, tip a sanci trhu",
          "tenis" in prompt and "tip: výhra" in prompt and "trh " in prompt)
    check("hazena: remiza = prohra", "remíza = prohra" in prompt)
    check("desetinna carka v kurzu", "kurz 1,16" in prompt)
    check("text pro chat je bez formatovani Telegramu", "\\" not in prompt and "*" not in prompt)

    class Http:
        def __init__(self):
            self.payloads = []

        def post(self, url, json=None, timeout=None):
            self.payloads.append(json)
            return SimpleNamespace(status_code=200, text="")

    notifier = TelegramNotifier(cfg)
    notifier._session = Http()
    notifier.send("cisty text", parse_mode="")
    check("cisty text se posila bez parse_mode",
          "parse_mode" not in notifier._session.payloads[0])

    def fails(**kwargs) -> str:
        try:
            make_cfg(**kwargs).validate(require_telegram=False)
        except ConfigError as exc:
            return str(exc)
        return ""

    check("LIST_PER_BAND mensi nez TIPS_PER_BAND se odchyti",
          "LIST_PER_BAND" in fails(list_per_band=2))
    check("bez AI se AI_POOL_PER_BAND nekontroluje",
          fails(ai_ranking=False, ai_pool_per_band=1) == "")


def test_scheduling() -> None:
    print("\n[11b] Planovani na GitHubu (jednou denne, ne pred 8:00)")
    import main as agent_main
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    prague = ZoneInfo("Europe/Prague")
    tlog = TipLog(path=Path("tips.selftest.json"))

    def reason(hour: int, minute: int, *, once=True, not_before=dtime(8, 0)):
        now = datetime(2026, 9, 15, hour, minute, tzinfo=prague)
        return agent_main.skip_reason(now, tlog, once_per_day=once,
                                      not_before=not_before)

    check("v 7:05 se jeste nic neposila", reason(7, 5) is not None)
    check("v 8:00 uz se posila", reason(8, 0) is None)
    check("v 10:05 se posila", reason(10, 5) is None)

    tlog.last_sent_day = "2026-09-15"
    check("kdyz uz dnes zprava byla, dalsi beh skonci", reason(9, 5) is not None)
    check("bez --once-per-day by se poslala znovu",
          reason(9, 5, once=False) is None)

    tlog.last_sent_day = "2026-09-14"
    check("vcerejsi zprava dnesni beh neblokuje", reason(8, 5) is None)

    check("bez prepinacu se nic nepreskakuje",
          reason(3, 0, once=False, not_before=None) is None)

    # Zimni cas: 07:05 UTC = 08:05 v Praze -> posila; 06:05 UTC = 07:05 -> ne.
    winter_early = datetime(2026, 12, 15, 6, 5, tzinfo=timezone.utc).astimezone(prague)
    winter_late = datetime(2026, 12, 15, 7, 5, tzinfo=timezone.utc).astimezone(prague)
    summer_early = datetime(2026, 7, 15, 6, 5, tzinfo=timezone.utc).astimezone(prague)
    empty = TipLog(path=Path("tips.selftest.json"))
    kwargs = dict(once_per_day=True, not_before=dtime(8, 0))
    check("zima: spusteni v 06:05 UTC (07:05 v Praze) se preskoci",
          agent_main.skip_reason(winter_early, empty, **kwargs) is not None)
    check("zima: spusteni v 07:05 UTC (08:05 v Praze) posle zpravu",
          agent_main.skip_reason(winter_late, empty, **kwargs) is None)
    check("leto: spusteni v 06:05 UTC (08:05 v Praze) posle zpravu",
          agent_main.skip_reason(summer_early, empty, **kwargs) is None)

    args = agent_main.parse_args(["--once-per-day", "--not-before", "08:00"])
    check("prepinace se nactou", args.once_per_day and args.not_before == dtime(8, 0))
    try:
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            agent_main.parse_args(["--not-before", "8h"])
        check("nesmyslny cas se odmitne", False)
    except SystemExit:
        check("nesmyslny cas se odmitne", True)

    workflow = Path(".github/workflows/denni-tipy.yml")
    check("workflow soubor existuje", workflow.exists())
    if workflow.exists():
        text = workflow.read_text(encoding="utf-8")
        check("workflow spousti agenta s hlidanim casu",
              "--once-per-day --not-before 07:55" in text)
        check("workflow bere klice ze secrets",
              all(f"secrets.{k}" in text
                  for k in ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")))
        check("workflow zapisuje denik zpet", "git push" in text and "tips.json" in text)
        check("workflow predava klice AI ze secrets",
              "secrets.ANTHROPIC_API_KEY" in text and "secrets.OPENAI_API_KEY" in text)
        check("workflow umi zkusebni beh bez odeslani",
              "dry_run" in text and "python main.py --dry-run" in text)
        check("workflow uklada i prubeh debaty", "git add debaty" in text)
        check("workflow ma cas na debatu", "timeout-minutes: 20" in text)
        check("selhani instalace anthropic nezabije cely beh",
              "grep -v '^anthropic'" in text)


def test_settings_split() -> None:
    print("\n[11c] Nastaveni: klice oddelene od settings.env")
    settings = Path("settings.env")
    check("settings.env existuje", settings.exists())
    if settings.exists():
        text = settings.read_text(encoding="utf-8")
        check("settings.env neobsahuje zadne klice",
              not any(line.split("=", 1)[0].strip() in
                      ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                       "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
                      for line in text.splitlines()))
        check("settings.env obsahuje pasma", "ODDS_BANDS=" in text)

    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    check(".env se nenahrava na GitHub", ".env" in ignored)
    check("denik tips.json se na GitHub nahrava", "tips.json" not in ignored)
    check("docasne soubory Google Drive se nenahravaji",
          ".tmp.driveupload/" in ignored)


def test_escaping() -> None:
    print("\n[12] MarkdownV2 escapovani")
    check("tecka se escapuje", esc("1.15") == "1\\.15")
    check("pomlcka se escapuje", esc("Man-City") == "Man\\-City")
    check("zavorky se escapuji", esc("(A)") == "\\(A\\)")
    check("podtrzitko se escapuje", esc("NO_BET") == "NO\\_BET")
    check("vykricnik se escapuje", esc("Ano!") == "Ano\\!")


def main() -> int:
    enable_utf8_stdout()
    print("=" * 62)
    print("SELFTEST -- offline kontrola bez API klice")
    print("=" * 62)
    test_devig()
    test_two_way_tip()
    test_no_odds_filter()
    test_ranking()
    test_bands()
    test_band_stats()
    test_reliability_checks()
    test_three_way_markets()
    test_mixed_market_structures()
    test_soccer_tip()
    test_tip_log()
    test_evaluate_score()
    test_sport_selection()
    test_config_validation()
    test_message()
    test_ai_ranking()
    test_gpt_client()
    test_ai_tracking()
    test_ai_message()
    test_chat_prompt()
    test_scheduling()
    test_settings_split()
    test_escaping()

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"NEUSPESNYCH KONTROL: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("VSECHNY KONTROLY PROSLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
