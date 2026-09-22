"""
ai_ranker.py -- spolecne casti AI preporadani kandidatu v pasmech.

Samotne rozhodovani je v ai_debate.py (debata agentu Gemini a Claude).
Tady je to, co na zpusobu rozhodovani nezavisi: oznaceni kandidatu (A1, A2...),
jejich popis pro AI, vysledne poradi (AIRanking) a jeho pouziti na tipy.

Bezpecnost provozu: kazde selhani AI vede na navrat ke kurzovemu poradi --
zprava nikdy nevypadne kvuli AI. Uspesnost AI se sleduje: do deniku se uklada
AI poradi i kurzove poradi (state.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from analyzer import Tip
from config import Config

log = logging.getLogger(__name__)

# USD za milion tokenu (vstup, vystup) -- jen pro odhad ceny do logu.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}
CACHE_WRITE_FACTOR = 1.25     # zapis do prompt cache stoji 1,25x vstup
CACHE_READ_FACTOR = 0.1       # cteni z cache 0,1x vstup

SPORT_CS = {
    "Tennis": "tenis",
    "Basketball": "basketbal",
    "Handball": "hazena",
    "Baseball": "baseball",
    "Ice Hockey": "hokej",
    "Soccer": "fotbal",
}

# Jak jisty je verdikt u tipu (Tip.ai_confidence) -- podle toho zprava vybere znacku.
AGREED = "agreed"         # 1. misto pasma, agenti se shodli
DISPUTED = "disputed"     # 1. misto pasma, neshodli se -- rozhodl prumer
SOLID = "solid"           # resers k zapasu nasla konkretni fakta
THIN = "thin"             # resers nasla malo nebo nic


class AIRankingError(RuntimeError):
    """Odpoved AI se nepodarilo pouzit -- volajici se vrati ke kurzovemu poradi."""


@dataclass
class AIVerdict:
    rank: int
    reason: str
    confidence: str


@dataclass
class AIRanking:
    """Vysledek AI: poradi kazdeho pasma a verdikt ke kazdemu kandidatu."""

    order: dict[str, list[str]]            # pasmo -> id kandidatu, nejlepsi prvni
    verdicts: dict[str, AIVerdict]         # id -> verdikt
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    notes: list[str] = field(default_factory=list)
    consensus: dict[str, bool] = field(default_factory=dict)   # pasmo -> shoda na 1.
    transcript: str = ""                   # prubeh debaty (Markdown)


# --------------------------------------------------------------------------- #
# Kandidati
# --------------------------------------------------------------------------- #
def candidate_ids(pool: dict[str, list[Tip]]) -> dict[str, Tip]:
    """id ("A1", "A2", "B1"...) -> tip. Cislo = poradi v pasmu podle sance."""
    ids: dict[str, Tip] = {}
    for band, tips in pool.items():
        for position, tip in enumerate(tips, start=1):
            ids[f"{band}{position}"] = tip
    return ids


def describe(cid: str, tip: Tip, cfg: Config) -> str:
    ev = tip.event
    sport = SPORT_CS.get(ev.sport_group, ev.sport_group or ev.sport_key)
    when = ev.commence_time.astimezone(cfg.tz).strftime("%d.%m. %H:%M")
    if ev.is_tennis:
        venue = "individual sport - home/away is meaningless here"
    elif tip.pick_team == ev.home_team:
        venue = "the favourite plays AT HOME"
    else:
        venue = "the favourite plays AWAY"
    draw = " (a draw counts as a loss for this tip)" if tip.has_draw else ""
    return (
        f"{cid} | {sport} | {ev.sport_title} | starts {when} ({cfg.timezone})\n"
        f"    tip: {tip.pick_team} beats {tip.opponent}{draw}; {venue}\n"
        f"    market: win probability {tip.prob * 100:.1f} %, typical odds "
        f"{tip.typical_odds:.2f}, {tip.n_books} bookmakers, "
        f"agreement {tip.agreement}"
    )


def match_name(tip: Tip) -> str:
    ev = tip.event
    return f"{ev.home_team} vs {ev.away_team}"


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write: int = 0,
    cache_read: int = 0,
) -> float | None:
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    return (
        input_tokens * prices[0]
        + cache_write * prices[0] * CACHE_WRITE_FACTOR
        + cache_read * prices[0] * CACHE_READ_FACTOR
        + output_tokens * prices[1]
    ) / 1_000_000


def make_anthropic_client(cfg: Config):
    """Import az tady: bez balicku 'anthropic' zbytek agenta dal funguje."""
    import anthropic

    return anthropic.Anthropic(
        api_key=cfg.anthropic_api_key, timeout=cfg.ai_timeout, max_retries=2)


# --------------------------------------------------------------------------- #
# Pouziti verdiktu
# --------------------------------------------------------------------------- #
def apply_ai_order(
    pool: dict[str, list[Tip]], ranking: AIRanking, cfg: Config
) -> tuple[dict[str, list[Tip]], dict[str, list[Tip]]]:
    """
    Vrati (vybrane, navic):
      vybrane -- v kazdem pasmu TIPS_PER_BAND tipu v poradi AI (jdou do zpravy),
      navic   -- tipy, ktere by podle ciste sance sly do zpravy, ale AI je
                 vyradila. Neposilaji se, jen se ulozi potichu, aby slo po case
                 porovnat, jestli mela AI pravdu.
    Tipum nastavi ai_rank, ai_reason a ai_confidence.
    """
    selected: dict[str, list[Tip]] = {}
    extras: dict[str, list[Tip]] = {}
    for band, tips in pool.items():
        ordered: list[Tip] = []
        for cid in ranking.order.get(band, []):
            position = int(cid[1:])
            tip = tips[position - 1]
            verdict = ranking.verdicts[cid]
            tip.ai_rank = verdict.rank
            tip.ai_reason = verdict.reason
            tip.ai_confidence = verdict.confidence
            ordered.append(tip)
        chosen = ordered[: cfg.tips_per_band]
        selected[band] = chosen
        odds_top = tips[: cfg.tips_per_band]
        extras[band] = [t for t in odds_top if not any(t is c for c in chosen)]
    return selected, extras


def log_verdicts(pool: dict[str, list[Tip]], ranking: AIRanking) -> None:
    """Do logu zapise, co AI rozhodla a proc -- pro pozdejsi kontrolu."""
    for band, tips in pool.items():
        for cid in ranking.order.get(band, []):
            tip = tips[int(cid[1:]) - 1]
            verdict = ranking.verdicts[cid]
            moved = int(cid[1:]) - verdict.rank      # kladne = AI tip povysila
            shift = "=" if moved == 0 else f"{moved:+d}"
            log.info("  AI %s%d -> %d (%s) %-24s %5.1f %%  [%s] %s",
                     band, int(cid[1:]), verdict.rank, shift,
                     tip.selection[:24], tip.prob * 100,
                     verdict.confidence, verdict.reason)
