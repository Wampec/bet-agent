"""
selftest.py -- offline kontrola bez API klice a bez internetu.

Spusteni:  python selftest.py

Overi de-vigging, konsensus kancelari, sestaveni a razeni tipu, kontroly
duveryhodnosti odhadu, denik tipu, vyhodnoceni vysledku, vyber souteci
(spotrebu kreditu) a sestaveni telegramove zpravy.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    check("vychozi pasma jsou 1,15-1,19 / 1,20-1,24 / 1,25 a vys",
          make_cfg().odds_bands == ((1.15, 1.19), (1.20, 1.24), (1.25, math.inf)))

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
          "1,15–1,19" in msg and "1,20–1,24" in msg and "1,25 a vys" in msg)
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


def test_settings_split() -> None:
    print("\n[11c] Nastaveni: klice oddelene od settings.env")
    settings = Path("settings.env")
    check("settings.env existuje", settings.exists())
    if settings.exists():
        text = settings.read_text(encoding="utf-8")
        check("settings.env neobsahuje zadne klice",
              not any(line.split("=", 1)[0].strip() in
                      ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
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
