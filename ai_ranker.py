"""
ai_ranker.py -- volitelny krok, ktery kandidaty z kazdeho pasma preradi AI.

Proc to existuje
----------------
Kurzy rikaji, kdo je favorit a jak moc. Nerikaji nic o stylu hry souperu, o
tom, jak se ktery tym dari doma a venku, o loni sezone... AI (Claude) o
tyhle veci vi a muze z kandidatu vybrat "nejtutovejsi" zapasy jinak, nez by je
seradila cista pravdepodobnost.

Co AI NEVI -- a proto nesmi predstirat
--------------------------------------
Krok bezi BEZ pristupu na internet. AI tedy nezna dnesni zraneni, sestavy,
prestupy, pocasi ani vysledky poslednich zapasu. Zadani ji proto vyslovne
zakazuje si je vymyslet a nuti ji brat kurzove poradi jako silny vychozi bod
(kurzy uz verejne informace, vcetne zraneni a formy, do velke miry zahrnuji).
Od kurzoveho poradi se ma odchylit jen z duvodu, ktery opravdu zna.

Bezpecnost provozu
------------------
Kazde selhani (chybi klic, sit, neplatna odpoved, vycerpane tokeny...) vede
na navrat ke kurzovemu poradi -- zprava nikdy nevypadne kvuli AI.

Uspesnost AI se sleduje: do deniku se uklada AI poradi i kurzove poradi,
takze po case jde porovnat, jestli AI vyber opravdu pomaha (state.py).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from analyzer import Tip, band_range_text
from config import Config

log = logging.getLogger(__name__)

# USD za milion tokenu (vstup, vystup) -- jen pro odhad ceny do logu.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}

SPORT_CS = {
    "Tennis": "tenis",
    "Basketball": "basketbal",
    "Handball": "hazena",
    "Baseball": "baseball",
    "Ice Hockey": "hokej",
    "Soccer": "fotbal",
}

CONFIDENCE_VALUES = ("high", "medium", "low")

SYSTEM_PROMPT = """\
You help rank sports betting tips for a personal daily digest. The user wants, \
in each odds band, the matches where the FAVOURITE is most reliably going to \
win ("nejtutovejsi" = the safest pick).

You receive candidate matches grouped into odds bands. Each candidate carries \
the bookmaker-derived win probability of the favourite (margin already removed, \
median across many bookmakers). Rank ALL candidates of each band from the most \
reliable favourite win to the least reliable.

HARD LIMITS - read carefully:
- You have NO internet access and NO live data. You do not know today's \
injuries, line-ups, transfers, suspensions, weather, or the results of recent \
matches. NEVER claim or imply that you do, and never invent them. If a factor \
depends on such information, ignore it.
- Use only knowledge you actually hold: general strength and playing style of \
the teams or players, how the styles match up, REAL home advantage (team sports \
only - in tennis "home/away" is just the order the bookmaker listed the players \
and means nothing), last season's level, fatigue that follows from the sport \
and schedule (e.g. very late kick-off, congested competitions), typical \
surface or venue effects when you know them.
- The market probability is a strong prior: it already prices in public \
information, including injuries and current form, far better than you can \
without live data. Depart from the market order ONLY for a concrete reason you \
can name and are confident about. When you have nothing specific, keep the \
market order (candidate numbers ascending). A vague hunch must not reorder \
anything.

For every candidate return:
- id: exactly as given (e.g. "A1"),
- reason: ONE short sentence in Czech (max ~130 characters) naming the deciding \
factor, or saying that nothing changes versus the market. No invented facts,
- confidence: "high" if you know these specific teams/players well and \
recently enough to justify the placement, "low" if you barely know them or \
your knowledge may be out of date (roster changes, obscure league), else \
"medium".

List every candidate id from the input exactly once, best first within its band.\
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "band": {"type": "string"},
                    "ranking": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "reason": {"type": "string"},
                                "confidence": {
                                    "type": "string",
                                    "enum": list(CONFIDENCE_VALUES),
                                },
                            },
                            "required": ["id", "reason", "confidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["band", "ranking"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bands"],
    "additionalProperties": False,
}


class AIRankingError(RuntimeError):
    """Odpoved AI se nepodarilo pouzit -- volajici se vrati ke kurzovemu poradi."""


@dataclass
class AIVerdict:
    rank: int
    reason: str
    confidence: str


@dataclass
class AIRanking:
    """Overena odpoved AI: poradi kazdeho pasma a verdikt ke kazdemu kandidatu."""

    order: dict[str, list[str]]            # pasmo -> id kandidatu, nejlepsi prvni
    verdicts: dict[str, AIVerdict]         # id -> verdikt
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Sestaveni zadani
# --------------------------------------------------------------------------- #
def candidate_ids(pool: dict[str, list[Tip]]) -> dict[str, Tip]:
    """id ("A1", "A2", "B1"...) -> tip. Cislo = poradi v pasmu podle sance."""
    ids: dict[str, Tip] = {}
    for band, tips in pool.items():
        for position, tip in enumerate(tips, start=1):
            ids[f"{band}{position}"] = tip
    return ids


def _describe(cid: str, tip: Tip, cfg: Config) -> str:
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


def build_prompt(pool: dict[str, list[Tip]], cfg: Config, now: datetime) -> str:
    bands = {
        key: cfg.odds_bands[ord(key) - ord("A")]
        for key in pool
        if len(key) == 1 and 0 <= ord(key) - ord("A") < len(cfg.odds_bands)
    }
    parts = [
        f"Today is {now.astimezone(cfg.tz):%A %d.%m.%Y}. "
        f"You will keep the best {cfg.tips_per_band} per band, so the ranking "
        "decides which candidates make the cut.",
    ]
    for band, tips in pool.items():
        if not tips:
            continue
        lo, hi = bands.get(band, (0.0, 0.0))
        parts.append(
            f"\nBAND {band} (typical odds {band_range_text(lo, hi, decimal_comma=False)})"
        )
        parts += [_describe(f"{band}{i}", tip, cfg) for i, tip in enumerate(tips, start=1)]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Zpracovani odpovedi
# --------------------------------------------------------------------------- #
def parse_response(
    data: Any, ids: dict[str, Tip], pool: dict[str, list[Tip]]
) -> tuple[dict[str, list[str]], dict[str, AIVerdict], list[str]]:
    """
    Overi a dopocita odpoved AI. Tolerantni k drobnym chybam, ale nikdy
    nevymysli poradi za AI:
      - neznamy id se ignoruje, duplikat se pocita jednou,
      - kandidati, kterych se AI nedotkla, jdou na konec v kurzovem poradi,
      - pasmo urcuje pismeno v id, ne popisek pasma (AI ho muze pojmenovat jinak).
    """
    if not isinstance(data, dict) or not isinstance(data.get("bands"), list):
        raise AIRankingError("odpoved nema pole 'bands'")

    order: dict[str, list[str]] = {band: [] for band in pool}
    verdicts: dict[str, AIVerdict] = {}
    notes: list[str] = []
    unknown = duplicate = 0

    for band_item in data["bands"]:
        if not isinstance(band_item, dict):
            continue
        for item in band_item.get("ranking") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id", "")).strip()
            if cid not in ids:
                unknown += 1
                continue
            band = cid[0]
            if cid in order[band]:
                duplicate += 1
                continue
            order[band].append(cid)
            confidence = str(item.get("confidence", "")).strip().lower()
            verdicts[cid] = AIVerdict(
                rank=len(order[band]),
                reason=" ".join(str(item.get("reason", "")).split()),
                confidence=confidence if confidence in CONFIDENCE_VALUES else "low",
            )

    if not verdicts:
        raise AIRankingError("AI nevratila zadneho platneho kandidata")

    missing_total = 0
    for band, tips in pool.items():
        for position in range(1, len(tips) + 1):
            cid = f"{band}{position}"
            if cid not in order[band]:
                order[band].append(cid)
                verdicts[cid] = AIVerdict(
                    rank=len(order[band]), reason="", confidence="low")
                missing_total += 1
    if unknown:
        notes.append(f"{unknown}x neznamy kandidat ignorovan")
    if duplicate:
        notes.append(f"{duplicate}x duplicita ignorovana")
    if missing_total:
        notes.append(f"{missing_total} kandidatu AI vynechala -- jdou na konec")
    return order, verdicts, notes


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000


# --------------------------------------------------------------------------- #
# Volani API
# --------------------------------------------------------------------------- #
def _make_client(cfg: Config):
    """Import az tady: bez balicku 'anthropic' zbytek agenta dal funguje."""
    import anthropic

    return anthropic.Anthropic(
        api_key=cfg.anthropic_api_key, timeout=cfg.ai_timeout, max_retries=2)


def skip_reason(
    pool: dict[str, list[Tip]], cfg: Config, client: Any | None = None
) -> str | None:
    """Proc se AI krok nema spustit (None = ma bezet). Pouziva i vyber tipu."""
    if not cfg.ai_ranking:
        return "AI_RANKING=false"
    if client is None and not cfg.anthropic_api_key:
        return "chybi ANTHROPIC_API_KEY"
    if not any(len(tips) >= 2 for tips in pool.values()):
        return "v zadnem pasmu nejsou aspon 2 zapasy, neni co radit"
    return None


def rank_with_ai(
    pool: dict[str, list[Tip]],
    cfg: Config,
    now: datetime,
    client: Any | None = None,
) -> AIRanking | None:
    """
    Zepta se AI na poradi kandidatu. Vraci None, kdyz je krok vypnuty, nema
    smysl, nebo selhal -- volajici pak pouzije kurzove poradi.

    `client` je jen pro testy (falesny klient); v provozu se vytvori sam.
    """
    reason = skip_reason(pool, cfg, client)
    if reason:
        log.info("AI preporadani preskoceno: %s.", reason)
        return None

    ids = candidate_ids(pool)
    try:
        api = client if client is not None else _make_client(cfg)
        response = api.messages.create(
            model=cfg.ai_model,
            max_tokens=cfg.ai_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(pool, cfg, now)}],
            output_config={
                "effort": cfg.ai_effort,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        )

        stop = getattr(response, "stop_reason", None)
        if stop in ("max_tokens", "refusal"):
            raise AIRankingError(f"AI skoncila predcasne ({stop})")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise AIRankingError("odpoved neobsahuje text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIRankingError(f"odpoved neni platny JSON: {exc}") from exc

        order, verdicts, notes = parse_response(data, ids, pool)
    except Exception as exc:  # noqa: BLE001 - jakekoli selhani = zpet ke kurzum
        log.warning("AI preporadani selhalo, pouziji kurzove poradi: %s: %s",
                    type(exc).__name__, exc)
        return None

    usage = getattr(response, "usage", None)
    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    ranking = AIRanking(
        order=order, verdicts=verdicts, model=cfg.ai_model,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=estimate_cost(cfg.ai_model, in_tok, out_tok), notes=notes,
    )
    cost = f", ~{ranking.cost_usd:.3f} USD" if ranking.cost_usd is not None else ""
    log.info("AI (%s): %s vstupnich + %s vystupnich tokenu%s.",
             cfg.ai_model, in_tok, out_tok, cost)
    for note in notes:
        log.warning("AI odpoved: %s", note)
    return ranking


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
