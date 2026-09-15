"""
analyzer.py -- matematicke jadro agenta.

Ukolem je odpovedet na jedinou otazku: **jaka je sance, ze favorit vyhraje?**

Postup:
  1. Z kurzu kazde sazkove kancelare se odstrani marze (de-vigging: Shinuv
     model / multiplicativni / mocninna metoda) -> "fair" pravdepodobnosti.
  2. Z jednotlivych kancelari se udela konsensus (median po vysledcich, pak
     normalizace) -- odolnejsi vuci jedne odlehle nabidce nez prosty prumer.
  3. Z konsensu se odvodi pravdepodobnost pozadovaneho trhu:
        tenis / basketbal / baseball / hokej  = p(favorit) z dvoucestneho h2h
        fotbal 1X (neprohra domacich)         = p(domaci) + p(remiza)
        fotbal DNB domaci                     = p(domaci) / (p(dom) + p(hoste))
  4. Vyradi se zapasy, u kterych se odhadu neda verit (malo kancelari nebo
     se prilis rozchazeji), a zbytek se seradi podle pravdepodobnosti.

CO SE ZAMERNE NEDELA
--------------------
Agent neresi vysi kurzu, hodnotu sazky (edge) ani velikost vkladu. Kurz se
uvadi jen informativne, aby slo tip najit u kancelare. Duvod je zadani:
uzivatele zajima sance na vyhru, penize si ridi sam.

Dusledek, ktery je poctive znat: samotny vyber favorita ma zapornou
ocekavanou hodnotu -- v kurzu je zapocitana marze kancelare. Vysoka
uspesnost jednotlivych tipu tedy sama o sobe neznamena zisk.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Sequence

from config import Config
from fetcher import Event

log = logging.getLogger(__name__)

MARKET_LABELS = {
    "double_chance": "neprohra domaciho favorita (1X)",
    "draw_no_bet": "vyhra domacich, pri remize vklad zpet",
    "h2h": "vyhra favorita",
}

# Faze, ve kterych zapas vypadava -- slouzi k diagnostice v logu i ve zprave.
STAGE_MARKET = "chybi trh"
STAGE_BOOKMAKERS = "malo kancelari"
STAGE_DISAGREEMENT = "kancelare se neshodnou"
STAGE_TIME = "prilis pozde"
STAGE_LOW_PROB = "nizka sance"
STAGE_ORDER = (
    STAGE_MARKET, STAGE_BOOKMAKERS, STAGE_DISAGREEMENT,
    STAGE_TIME, STAGE_LOW_PROB,
)


# --------------------------------------------------------------------------- #
# Datove struktury
# --------------------------------------------------------------------------- #
@dataclass
class Tip:
    """Jeden navrzeny tip."""

    event: Event
    market: str            # h2h | double_chance | draw_no_bet
    selection: str         # co presne sazet, napr. "Alcaraz"
    pick_team: str         # tym / hrac pro vyhodnoceni vysledku
    prob: float            # konsensualni sance na vyhru (bez marze)
    odds: float            # nejlepsi nalezeny kurz -- jen informativne
    bookmaker: str         # kde ten kurz byl
    n_books: int           # kolik kancelari tvori konsensus
    dispersion: float      # sm. odchylka odhadu mezi kancelarmi
    n_outcomes: int = 2    # 3 = trh s remizou (hazena, hokej v zakl. dobe)
    # Median kurzu favorita napric kancelarmi. Podle nej se tip zaradi do
    # pasma, protoze odpovida tomu, co nabizi bezna kancelar.
    median_odds: float = 0.0

    @property
    def typical_odds(self) -> float:
        return self.median_odds or self.odds

    @property
    def opponent(self) -> str:
        home, away = self.event.home_team, self.event.away_team
        return away if self.pick_team == home else home

    @property
    def has_draw(self) -> bool:
        """Trojcestny trh: remiza je prohrana sazka, ne vracen vklad."""
        return self.n_outcomes >= 3

    @property
    def market_label(self) -> str:
        if self.market == "h2h" and self.has_draw:
            return "vyhra favorita (remiza = prohra)"
        return MARKET_LABELS.get(self.market, self.market)

    @property
    def agreement(self) -> str:
        """Shoda kancelari slovy -- srozumitelnejsi nez cislo."""
        if self.dispersion <= 0.015:
            return "vysoka"
        if self.dispersion <= 0.035:
            return "stredni"
        return "nizka"


@dataclass
class Rejection:
    """Duvod vyrazeni zapasu -- pro log a diagnostiku."""

    label: str
    reason: str
    stage: str = STAGE_MARKET


@dataclass
class AnalysisResult:
    tips: list[Tip]                # serazene od nejvyssi sance
    considered: int
    rejections: list[Rejection]
    funnel: dict[str, int] = field(default_factory=dict)

    @property
    def best(self) -> Tip | None:
        return self.tips[0] if self.tips else None

    def funnel_line(self) -> str:
        parts = [
            f"{stage} {self.funnel[stage]}"
            for stage in STAGE_ORDER
            if self.funnel.get(stage)
        ]
        return " | ".join(parts) if parts else "zadna zamitnuti"


# --------------------------------------------------------------------------- #
# 1. De-vigging (odstraneni marze kancelare)
# --------------------------------------------------------------------------- #
def implied_probs(odds: dict[str, float]) -> dict[str, float]:
    return {name: 1.0 / price for name, price in odds.items() if price > 1.0}


def devig(
    odds: dict[str, float], method: str = "shin", target_sum: float = 1.0
) -> dict[str, float]:
    """
    Odstrani marzi kancelare z kurzu jednoho trhu.

    target_sum = 1.0 pro bezne trhy; 2.0 pro dvojitou sanci, kde se kazdy
    vysledek zapasu vyskytuje ve dvou ze tri nabidek.
    """
    raw = implied_probs(odds)
    if len(raw) < 2:
        return {}
    booksum = sum(raw.values())
    if booksum <= 0:
        return {}

    if not math.isclose(target_sum, 1.0):
        return {k: v * target_sum / booksum for k, v in raw.items()}
    if booksum <= 1.0:
        # Zaporna marze (arbitraz nebo nekompletni trh) -- jen normalizace.
        return {k: v / booksum for k, v in raw.items()}

    if method == "multiplicative":
        return _devig_multiplicative(raw, booksum)
    if method == "power":
        return _devig_power(raw)
    return _devig_shin(raw, booksum)


def _devig_multiplicative(raw: dict[str, float], booksum: float) -> dict[str, float]:
    """Nejjednodussi metoda: proporcionalni skalovani. Favority mirne nadhodnocuje."""
    return {k: v / booksum for k, v in raw.items()}


def _devig_shin(raw: dict[str, float], booksum: float) -> dict[str, float]:
    """
    Shinuv model (1993): predpoklada podil informovanych sazkaru z.
    Marzi ubira vic u outsideru, coz odpovida realnemu favourite-longshot biasu.
    """

    def probs_for(z: float) -> dict[str, float]:
        denom = 2.0 * (1.0 - z)
        return {
            k: (math.sqrt(z * z + 4.0 * (1.0 - z) * v * v / booksum) - z) / denom
            for k, v in raw.items()
        }

    lo, hi = 0.0, 0.99
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if sum(probs_for(mid).values()) > 1.0:
            lo = mid
        else:
            hi = mid
    result = probs_for((lo + hi) / 2.0)
    total = sum(result.values())
    if total <= 0:
        return _devig_multiplicative(raw, booksum)
    return {k: v / total for k, v in result.items()}


def _devig_power(raw: dict[str, float]) -> dict[str, float]:
    """Mocninna metoda: hleda k tak, aby soucet p_i^k byl roven 1."""
    lo, hi = 1.0, 12.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if sum(v ** mid for v in raw.values()) > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    result = {key: v ** k for key, v in raw.items()}
    total = sum(result.values())
    return {key: v / total for key, v in result.items()}


# --------------------------------------------------------------------------- #
# 2. Konsensus napric kancelarmi
# --------------------------------------------------------------------------- #
def consensus_probs(
    books: Sequence[dict[str, Any]],
    method: str,
    target_sum: float = 1.0,
) -> tuple[dict[str, float], list[dict[str, float]]]:
    """
    Vrati (konsensualni pravdepodobnosti, odhad za kazdou kancelar).
    Konsensus = median po vysledcich, nasledne prenormovany na target_sum.
    """
    per_book: list[dict[str, float]] = []
    for entry in books:
        fair = devig(entry.get("outcomes", {}), method, target_sum)
        if fair:
            per_book.append(fair)
    if not per_book:
        return {}, []

    names = {name for fair in per_book for name in fair}
    medians: dict[str, float] = {}
    for name in names:
        values = [fair[name] for fair in per_book if name in fair]
        if values:
            medians[name] = statistics.median(values)

    total = sum(medians.values())
    if total <= 0:
        return {}, per_book
    return {k: v * target_sum / total for k, v in medians.items()}, per_book


def best_price(
    books: Sequence[dict[str, Any]], outcome_matcher
) -> tuple[float, str, float]:
    """
    Vrati (nejvyssi kurz, kancelar, median kurzu) na dany vysledek.

    Nejvyssi kurz casto nabizi burza (Betfair, Matchbook), na kterou uzivatel
    nemusi mit pristup. Median lip odpovida bezne kancelari, proto se podle
    nej zarazuje do pasem.
    """
    prices: list[tuple[float, str]] = []
    for entry in books:
        price = outcome_matcher(entry.get("outcomes", {}))
        if price and price > 1.0:
            prices.append((price, entry.get("bookmaker", "?")))
    if not prices:
        return 0.0, "", 0.0
    best, book = max(prices, key=lambda pair: pair[0])
    return best, book, statistics.median(p for p, _ in prices)


# --------------------------------------------------------------------------- #
# Kurzova pasma
# --------------------------------------------------------------------------- #
REST_BELOW = "pod"      # kurz pod nejnizsim pasmem -- uklada se potichu
REST_ABOVE = "nad"      # kurz nad nejvyssim pasmem -- uklada se potichu
REST_BETWEEN = "mezi"   # mezera mezi pasmy (jen kdyz jsou pasma nastavena s mezerou)
REST_KEYS = (REST_BELOW, REST_ABOVE, REST_BETWEEN)


def band_key(index: int) -> str:
    return chr(ord("A") + index)


def band_range_text(lo: float, hi: float, *, decimal_comma: bool = True) -> str:
    """'1,15–1,19' nebo '1,25 a vys' pro pasmo bez horni hranice."""
    def fmt(value: float) -> str:
        text = f"{value:.2f}"
        return text.replace(".", ",") if decimal_comma else text

    if math.isinf(hi):
        return f"{fmt(lo)} a vys"
    return f"{fmt(lo)}–{fmt(hi)}"


def round_odds(value: float) -> float:
    """Zaokrouhli kurz na setiny matematicky (1,245 -> 1,25), jak ho zobrazuji kancelare."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def band_for_odds(odds: float, bands: Sequence[tuple[float, float]]) -> str:
    """Klic pasma (A, B, ...) nebo pod / nad / mezi pro kurz mimo pasma."""
    rounded = round_odds(odds)
    for index, (lo, hi) in enumerate(bands):
        if lo <= rounded <= hi:
            return band_key(index)
    if rounded < bands[0][0]:
        return REST_BELOW
    if rounded > bands[-1][1]:
        return REST_ABOVE
    return REST_BETWEEN


def select_by_band(tips: Sequence[Tip], cfg: Config) -> dict[str, list[Tip]]:
    """
    Rozdeli tipy do pasem a v kazdem necha TIPS_PER_BAND s nejvyssi sanci.
    Vraci i tichy zbytek (pod / nad / mezi). Tipy musi byt serazene od
    nejvyssi sance -- tak je vraci analyze().
    """
    keys = [band_key(i) for i in range(len(cfg.odds_bands))] + list(REST_KEYS)
    selected: dict[str, list[Tip]] = {key: [] for key in keys}
    for tip in tips:
        key = band_for_odds(tip.typical_odds, cfg.odds_bands)
        if len(selected[key]) < cfg.tips_per_band:
            selected[key].append(tip)
    return selected


# --------------------------------------------------------------------------- #
# 3. Rozpoznani vysledku v ruznych pojmenovanich
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").replace("/", " ").split())


def _draw_key(outcomes: dict[str, float]) -> str | None:
    for name in outcomes:
        if _norm(name) in {"draw", "tie", "remiza"}:
            return name
    return None


def match_h2h(outcomes: dict[str, float], team: str) -> float | None:
    for name, price in outcomes.items():
        if _norm(name) == _norm(team):
            return price
    return None


def is_double_chance_home_name(name: str, home: str, away: str) -> bool:
    """
    Rozpozna nabidku 1X. Kancelare pouzivaji vic pojmenovani
    ("1X", "Arsenal/Draw", "Arsenal or Draw", "Home/Draw").
    """
    norm = _norm(name)
    if norm in {"1x", "home draw", "draw home"}:
        return True
    mentions_home = _norm(home) in norm
    mentions_away = _norm(away) in norm
    mentions_draw = any(word in norm for word in ("draw", "tie", "remiza"))
    return mentions_home and mentions_draw and not mentions_away


def match_double_chance_home(
    outcomes: dict[str, float], home: str, away: str
) -> float | None:
    for name, price in outcomes.items():
        if is_double_chance_home_name(name, home, away):
            return price
    return None


# --------------------------------------------------------------------------- #
# 4. Sestaveni tipu
# --------------------------------------------------------------------------- #
def _dispersion(per_book: list[dict[str, float]], key_fn) -> float:
    values = []
    for fair in per_book:
        try:
            value = key_fn(fair)
        except (KeyError, ZeroDivisionError, TypeError):
            continue
        if value is not None:
            values.append(value)
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


def build_h2h_tip(
    event: Event, cfg: Config, rejections: list[Rejection]
) -> Tip | None:
    """
    Vyhra favorita v zapase -- tenis, basketbal, hazena a dalsi.

    U sportu, kde je mozna remiza (hazena, hokej v zakladni hraci dobe),
    je trh tricestny. Tip pak znamena "favorit vyhraje", nikoli "neprohraje" --
    remiza je prohrana sazka. Spoctena pravdepodobnost to uz zohlednuje,
    protoze remiza ukrajuje ze sance favorita.
    """
    books = event.markets.get("h2h", [])
    if not books:
        rejections.append(Rejection(event.label, "chybi pouzitelny trh", STAGE_MARKET))
        return None

    # Kancelare musi kotovat stejnou strukturu trhu. U nekterych sportu cast
    # kancelari nabizi dvoucestny trh (vcetne prodlouzeni) a cast tricestny
    # (jen zakladni hraci doba) -- jsou to ruzne sazky a median pres ne
    # dohromady by neznamenal nic. Bereme proto jen prevazujici strukturu.
    structure: dict[int, int] = {}
    for entry in books:
        size = len(entry.get("outcomes", {}))
        structure[size] = structure.get(size, 0) + 1
    dominant = max(structure, key=lambda size: (structure[size], size))
    if len(structure) > 1:
        dropped = sum(n for size, n in structure.items() if size != dominant)
        log.debug("%s: %s kancelari kotuje jinou strukturu trhu -- vynechavam.",
                  event.label, dropped)
        books = [b for b in books if len(b.get("outcomes", {})) == dominant]

    if cfg.two_way_only and dominant != 2:
        rejections.append(Rejection(
            event.label, f"trh ma {dominant} vysledky vcetne remizy", STAGE_MARKET))
        return None

    consensus, per_book = consensus_probs(books, cfg.devig_method)
    if not consensus or len(consensus) < 2:
        rejections.append(Rejection(event.label, "chybi pouzitelny trh", STAGE_MARKET))
        return None

    # Remiza sama o sobe neni tip -- favorit se hleda jen mezi tymy a hraci.
    draw_name = _draw_key(consensus)
    contenders = {k: v for k, v in consensus.items() if k != draw_name}
    if not contenders:
        rejections.append(Rejection(event.label, "chybi pouzitelny trh", STAGE_MARKET))
        return None

    favourite = max(contenders, key=contenders.get)
    odds, book, median = best_price(books, lambda o: match_h2h(o, favourite))
    if not odds:
        rejections.append(Rejection(event.label, "chybi kurz favorita", STAGE_MARKET))
        return None

    return Tip(
        event=event,
        market="h2h",
        selection=favourite,
        pick_team=favourite,
        prob=consensus[favourite],
        odds=odds,
        bookmaker=book,
        n_books=len(per_book),
        dispersion=_dispersion(per_book, lambda f: f.get(favourite)),
        n_outcomes=dominant,
        median_odds=median,
    )


def build_soccer_tip(
    event: Event, cfg: Config, rejections: list[Rejection]
) -> Tip | None:
    """
    Fotbal: nejjistejsi je 1X (domaci favorit neprohraje), pripadne DNB.
    Vybere se ta varianta, ktera ma vyssi pravdepodobnost.
    """
    consensus, per_book = consensus_probs(
        event.markets.get("h2h", []), cfg.devig_method)
    if not consensus:
        rejections.append(Rejection(event.label, "chybi pouzitelny trh", STAGE_MARKET))
        return None

    draw_name = _draw_key(consensus)
    p_home = consensus.get(event.home_team)
    p_away = consensus.get(event.away_team)
    p_draw = consensus.get(draw_name) if draw_name else None
    if p_home is None or p_away is None or p_draw is None:
        rejections.append(Rejection(event.label, "trh neni tricestny", STAGE_MARKET))
        return None

    if cfg.soccer_home_favourite_only and not (p_home > p_away and p_home > p_draw):
        rejections.append(Rejection(
            event.label, f"favorit nehraje doma (domaci {p_home:.0%})", STAGE_MARKET))
        return None

    candidates: list[Tip] = []

    books_dc = event.markets.get("double_chance", [])
    odds_dc, book_dc, median_dc = best_price(
        books_dc,
        lambda o: match_double_chance_home(o, event.home_team, event.away_team),
    )
    if odds_dc:
        candidates.append(Tip(
            event=event, market="double_chance",
            selection=f"{event.home_team} 1X",
            pick_team=event.home_team,
            prob=p_home + p_draw, odds=odds_dc, bookmaker=book_dc,
            median_odds=median_dc,
            n_books=len(per_book),
            dispersion=_dispersion(
                per_book,
                lambda f: f.get(event.home_team, 0.0)
                + (f.get(draw_name, 0.0) if draw_name else 0.0)),
        ))

    books_dnb = event.markets.get("draw_no_bet", [])
    odds_dnb, book_dnb, median_dnb = best_price(
        books_dnb, lambda o: match_h2h(o, event.home_team))
    denom = p_home + p_away
    if odds_dnb and denom > 0:
        candidates.append(Tip(
            event=event, market="draw_no_bet",
            selection=f"{event.home_team} (DNB)",
            pick_team=event.home_team,
            prob=p_home / denom, odds=odds_dnb, bookmaker=book_dnb,
            median_odds=median_dnb,
            n_books=len(per_book),
            dispersion=_dispersion(
                per_book,
                lambda f: (
                    f[event.home_team] / (f[event.home_team] + f[event.away_team])
                    if f.get(event.home_team) and f.get(event.away_team) else None)),
        ))

    if not candidates:
        rejections.append(Rejection(
            event.label, "kancelare nenabizi 1X ani DNB", STAGE_MARKET))
        return None
    return max(candidates, key=lambda t: t.prob)


# --------------------------------------------------------------------------- #
# 5. Kontrola duveryhodnosti odhadu
# --------------------------------------------------------------------------- #
def check_tip(
    tip: Tip, cfg: Config, now: datetime | None = None
) -> tuple[str, str] | None:
    """
    Vrati (faze, duvod) pri vyrazeni, nebo None kdyz tip projde.

    Nekontroluje se kurz ani hodnota sazky -- jen to, jestli se da odhadu
    pravdepodobnosti verit a jestli na sazku zbyva cas.
    """
    if tip.n_books < cfg.min_bookmakers:
        return STAGE_BOOKMAKERS, (
            f"jen {tip.n_books} kancelari (min. {cfg.min_bookmakers})"
        )
    if tip.dispersion > cfg.max_prob_dispersion:
        return STAGE_DISAGREEMENT, (
            f"kancelare se rozchazeji o {tip.dispersion * 100:.1f} p.b. "
            f"(max {cfg.max_prob_dispersion * 100:.0f})"
        )
    now = now or datetime.now(timezone.utc)
    minutes_left = (tip.event.commence_time - now).total_seconds() / 60.0
    if minutes_left < cfg.min_minutes_before_start:
        return STAGE_TIME, f"do zacatku zbyva jen {minutes_left:.0f} min"
    if tip.prob < cfg.min_tip_prob:
        return STAGE_LOW_PROB, (
            f"sance {tip.prob:.1%} < pozadovanych {cfg.min_tip_prob:.1%}"
        )
    return None


# --------------------------------------------------------------------------- #
# 6. Hlavni vstupni bod modulu
# --------------------------------------------------------------------------- #
def prefilter_soccer_events(events: Sequence[Event], cfg: Config) -> list[Event]:
    """
    Levny predvyber pred placenym dotazem na trhy 1X / DNB: seradi zapasy
    podle sance, ze domaci neprohraji, a vrati je od nejjistejsiho.
    """
    scored: list[tuple[float, Event]] = []
    for event in events:
        consensus, _ = consensus_probs(event.markets.get("h2h", []), cfg.devig_method)
        if not consensus:
            continue
        draw_name = _draw_key(consensus)
        p_home = consensus.get(event.home_team)
        p_away = consensus.get(event.away_team)
        p_draw = consensus.get(draw_name) if draw_name else None
        if None in (p_home, p_away, p_draw):
            continue
        if cfg.soccer_home_favourite_only and not (p_home > p_away and p_home > p_draw):
            continue
        scored.append((p_home + p_draw, event))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [event for _, event in scored]


def analyze(
    events: Sequence[Event], cfg: Config, now: datetime | None = None
) -> AnalysisResult:
    """Projde zapasy a vrati tipy serazene od nejvyssi sance na vyhru."""
    now = now or datetime.now(timezone.utc)
    rejections: list[Rejection] = []
    tips: list[Tip] = []

    for event in events:
        if event.is_soccer:
            tip = build_soccer_tip(event, cfg, rejections) if cfg.enable_soccer else None
        else:
            tip = build_h2h_tip(event, cfg, rejections)
        if tip is None:
            continue
        failed = check_tip(tip, cfg, now)
        if failed:
            stage, reason = failed
            rejections.append(Rejection(event.label, reason, stage))
            continue
        tips.append(tip)

    # Hlavni kriterium je sance na vyhru; pri shode rozhoduje vetsi shoda
    # kancelari a pak jejich pocet.
    tips.sort(key=lambda t: (t.prob, -t.dispersion, t.n_books), reverse=True)

    funnel: dict[str, int] = {}
    for rej in rejections:
        funnel[rej.stage] = funnel.get(rej.stage, 0) + 1

    log.info("Pouzitelnych tipu: %s z %s zapasu.", len(tips), len(events))
    if funnel:
        log.info("Trychtyr zamitnuti: %s", " | ".join(
            f"{stage} {funnel[stage]}" for stage in STAGE_ORDER if funnel.get(stage)))
    for key, picked in select_by_band(tips, cfg).items():
        for tip in picked:
            log.info("  [%-4s] %-28s %5.1f %%  kurz %.2f (median %.2f)  %s",
                     key, tip.selection[:28], tip.prob * 100, tip.odds,
                     tip.typical_odds, tip.event.sport_title)

    return AnalysisResult(
        tips=tips, considered=len(events), rejections=rejections, funnel=funnel
    )
