"""
ai_debate.py -- dva AI agenti se v kazdem pasmu dohaduji, ktery zapas je
nejtutovejsi.

Postup
------
1. RESERS: Gemini s vyhledavanim Google dohleda ke kandidatum kazdeho pasma
   zraneni, formu, vzajemne zapasy, program a unavu, motivaci, pocasi.
   Stejne podklady pak dostanou oba agenti.
2. UVODNI VERDIKTY: Gemini (ANALYTIK) a Claude (SKEPTIK) nezavisle na sobe
   ohodnoti kazdeho kandidata: vlastni odhad sance na vyhru (0-100) + argument.
3. DEBATA: v pasmech, kde se neshodnou na 1. miste, se stridaji v odpovedich
   (Gemini, Claude, Gemini...) -- kazdy vidi posledni verdikt a argumenty
   soupere -- dokud se neshodnou, nejvys AI_MAX_TURNS odpovedi.
4. VERDIKT POCITA KOD, ne treti AI (ta by mohla "ustoupit", jen aby byl
   konec): 1. misto = na cem se shodli; kdyz se neshodli, nejvyssi prumer
   obou odhadu a zprava to oznaci. Zbytek pasma podle prumeru obou odhadu.

Kazde selhani agenta vede na navrat ke kurzovemu poradi (zprava to rekne).
Selhani reserse jednoho pasma debatu nezastavi -- agenti se dozvi, ze data
nemaji, a zprava zapasy oznaci jako "bez dat".
Prubeh debaty se vraci jako Markdown (AIRanking.transcript), main.py ho
uklada do debaty/RRRR-MM-DD.md.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ai_ranker import (
    AGREED, DISPUTED, SOLID, THIN,
    AIRanking, AIRankingError, AIVerdict,
    describe, estimate_cost, make_anthropic_client, match_name,
)
from analyzer import Tip, band_range_text
from config import Config
from gemini import GeminiClient, GeminiError

log = logging.getLogger(__name__)

MAX_DOSSIER_CHARS = 7000      # na pasmo -- drzi cenu vstupu na uzde

# --------------------------------------------------------------------------- #
# Zadani
# --------------------------------------------------------------------------- #
RESEARCH_SYSTEM = """\
You are a sports research assistant. Use Google Search to collect FACTS about \
upcoming matches. You give no predictions and no betting advice - only \
verifiable, dated facts from the most recent sources you can find. Write in \
Czech.\
"""

RESEARCH_TASK = """\
Today is {today}. Research each match below. For every match look for:
- injuries, suspensions, illness, expected line-ups, key players rested,
- the last ~5 results of both sides (with dates and scores) and recent head-to-head,
- team sports: home and away record this season,
- schedule: days of rest, travel, back-to-back games, an important match \
coming next (rotation risk),
- motivation: standings, what is at stake (playoffs, relegation, nothing),
- tennis: surface, recent results on it, fitness, recent retirements or withdrawals,
- weather if the match is outdoors and weather matters (e.g. baseball).

Output format, for each match exactly:
### <id> <home> vs <away>
- <fact with date> (<source name>)
- ...
- Nenalezeno: <what you could not find>

3 to 8 bullet points per match, concrete and short. Never guess - if you did \
not find something, put it under "Nenalezeno".

Matches:
{matches}\
"""

COMMON_RULES = """\
Context: a personal daily digest of sports tips. In every odds band the user \
gets the matches where the FAVOURITE is most likely to win ("nejtutovejsi" = \
the safest pick). You and another AI agent must agree on the safest pick of \
each band; the other candidates are ranked by your estimates too.

Rules for both agents:
- Your evidence is (a) the market data given with every candidate, (b) the \
research dossier gathered from the web today, (c) well-established general \
knowledge. Nothing else. Never invent injuries, results, line-ups or weather. \
If the dossier says nothing useful about a match, say so and stay close to \
the market.
- The dossier comes from a web search and can be incomplete, stale or wrong. \
Prefer dated, specific facts; be careful with rumours.
- The market probability (bookmaker margin removed, median of many \
bookmakers) already prices in public information. Depart from it for \
concrete reasons only.
- "safety" = YOUR estimate of the probability in percent (0-100) that the tip \
wins. Where stated, a draw counts as a loss. In tennis home/away means nothing.
- "argument": one or two sentences in Czech (max ~200 characters) with the \
deciding fact(s) for that candidate.
- "evidence": "solid" if the dossier has relevant, specific facts about this \
match, "thin" if it has little or nothing.
- Rate every candidate of every band you are asked about, each id exactly once.
- "reply_to_opponent": in Czech; empty string in your opening verdict.\
"""

ANALYST_ROLE = """\
You are the ANALYST (agent Gemini). You build the case from data: recent form, \
head-to-head, home and away records, schedule and rest, standings and \
motivation, the market. You are cold and numerical and not impressed by big \
names: a favourite with a strong data case beats a famous one with a weak case.\
"""

SKEPTIC_ROLE = """\
You are the SKEPTIC (agent Claude), a risk auditor. Your job is to find the \
reasons a favourite could fail: absences, rotation, fatigue and congested \
schedule, travel, lack of motivation, bad style match-up, weather, thin or \
contradictory data. Treat every pick as a possible trap until the facts clear \
it. You still rank everything - the question is which favourite is the LEAST \
likely to fail, not whether to bet at all.\
"""

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "band": {"type": "string"},
                    "reply_to_opponent": {"type": "string"},
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "safety": {"type": "integer"},
                                "argument": {"type": "string"},
                                "evidence": {"type": "string", "enum": [SOLID, THIN]},
                            },
                            "required": ["id", "safety", "argument", "evidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["band", "reply_to_opponent", "candidates"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["bands"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Verdikty
# --------------------------------------------------------------------------- #
@dataclass
class Rating:
    safety: int | None        # None = agent kandidata vynechal
    argument: str = ""
    evidence: str = THIN


@dataclass
class BandVerdict:
    """Nazor jednoho agenta na jedno pasmo."""

    ratings: dict[str, Rating]
    reply: str = ""

    def order(self) -> list[str]:
        """Od nejbezpecnejsiho; shoda odhadu -> vyssi sance podle trhu."""
        return sorted(self.ratings, key=lambda cid: (
            -(self.ratings[cid].safety if self.ratings[cid].safety is not None else -1),
            int(cid[1:])))

    def pick(self) -> str:
        return self.order()[0]


def parse_verdict(
    data: Any, band_ids: dict[str, list[str]]
) -> tuple[dict[str, BandVerdict], list[str]]:
    """
    Overi odpoved agenta. `band_ids` = pasma, na ktera se ptame, a jejich id.
    Vrati verdikty jen pro pasma, kde agent ohodnotil aspon jednoho kandidata;
    vynechane kandidaty doplni na konec (bez odhadu), nic za agenta nevymysli.
    """
    if not isinstance(data, dict) or not isinstance(data.get("bands"), list):
        raise AIRankingError("odpoved nema pole 'bands'")
    found: dict[str, dict[str, Rating]] = {band: {} for band in band_ids}
    replies: dict[str, str] = {}
    notes: list[str] = []
    ignored = 0
    for item in data["bands"]:
        if not isinstance(item, dict):
            continue
        item_bands: set[str] = set()
        for cand in item.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            cid = str(cand.get("id", "")).strip().upper()
            band = cid[:1]
            if band not in band_ids or cid not in band_ids[band] or cid in found[band]:
                ignored += 1
                continue
            try:
                safety = max(0, min(100, int(round(float(cand.get("safety"))))))
            except (TypeError, ValueError):
                ignored += 1
                continue
            evidence = str(cand.get("evidence", "")).strip().lower()
            found[band][cid] = Rating(
                safety=safety,
                argument=" ".join(str(cand.get("argument", "")).split()),
                evidence=evidence if evidence in (SOLID, THIN) else THIN,
            )
            item_bands.add(band)
        reply = " ".join(str(item.get("reply_to_opponent", "")).split())
        for band in item_bands:
            replies.setdefault(band, reply)

    verdicts: dict[str, BandVerdict] = {}
    for band, ids in band_ids.items():
        if not found[band]:
            continue
        missing = [cid for cid in ids if cid not in found[band]]
        for cid in missing:
            found[band][cid] = Rating(safety=None)
        if missing:
            notes.append(f"pasmo {band}: {len(missing)} kandidatu bez odhadu")
        verdicts[band] = BandVerdict(ratings=found[band], reply=replies.get(band, ""))
    if ignored:
        notes.append(f"{ignored}x neplatny nebo duplicitni kandidat ignorovan")
    return verdicts, notes


# --------------------------------------------------------------------------- #
# Agenti -- kazdy si drzi vlastni konverzaci
# --------------------------------------------------------------------------- #
class ClaudeAgent:
    name = "Claude"

    def __init__(self, client: Any, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self.system = f"{SKEPTIC_ROLE}\n\n{COMMON_RULES}"
        self.messages: list[dict] = []
        self.input_tokens = self.output_tokens = 0
        self.cache_write = self.cache_read = 0

    def ask(self, prompt: str) -> Any:
        block: dict[str, Any] = {"type": "text", "text": prompt}
        if not self.messages:
            # Uvodni zadani s reserci je dlouhe a v kazdem kole stejne -> cache.
            block["cache_control"] = {"type": "ephemeral"}
        self.messages.append({"role": "user", "content": [block]})
        response = self.client.messages.create(
            model=self.cfg.ai_model,
            max_tokens=self.cfg.ai_max_tokens,
            system=self.system,
            messages=self.messages,
            output_config={
                "effort": self.cfg.ai_effort,
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
        )
        usage = getattr(response, "usage", None)
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_write += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.cache_read += int(getattr(usage, "cache_read_input_tokens", 0) or 0)

        stop = getattr(response, "stop_reason", None)
        if stop in ("max_tokens", "refusal"):
            raise AIRankingError(f"Claude skoncil predcasne ({stop})")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise AIRankingError("Claude: odpoved neobsahuje text")
        self.messages.append({"role": "assistant", "content": text})
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIRankingError(f"Claude: odpoved neni platny JSON: {exc}") from exc

    @property
    def cost_usd(self) -> float | None:
        return estimate_cost(self.cfg.ai_model, self.input_tokens, self.output_tokens,
                             self.cache_write, self.cache_read)


class GeminiAgent:
    name = "Gemini"

    def __init__(self, client: GeminiClient) -> None:
        self.client = client
        self.system = f"{ANALYST_ROLE}\n\n{COMMON_RULES}"
        self.contents: list[dict] = []
        self.input_tokens = self.output_tokens = 0

    def ask(self, prompt: str) -> Any:
        self.contents.append({"role": "user", "parts": [{"text": prompt}]})
        try:
            reply = self.client.generate(
                self.contents, system=self.system, schema=VERDICT_SCHEMA)
        except GeminiError as exc:
            raise AIRankingError(f"Gemini: {exc}") from exc
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.contents.append({"role": "model", "parts": [{"text": reply.text}]})
        try:
            return json.loads(reply.text)
        except json.JSONDecodeError as exc:
            raise AIRankingError(f"Gemini: odpoved neni platny JSON: {exc}") from exc


# --------------------------------------------------------------------------- #
# Resers
# --------------------------------------------------------------------------- #
@dataclass
class Dossier:
    text: str
    ok: bool
    sources: list[tuple[str, str]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


def research_band(
    gemini: GeminiClient, band: str, tips: list[Tip], cfg: Config, now: datetime
) -> Dossier:
    matches = "\n".join(
        f"{band}{i} | {match_name(t)} | {t.event.sport_title} | "
        f"{t.event.commence_time.astimezone(cfg.tz):%d.%m. %H:%M}"
        for i, t in enumerate(tips, start=1))
    prompt = RESEARCH_TASK.format(
        today=f"{now.astimezone(cfg.tz):%A %d.%m.%Y}", matches=matches)
    try:
        reply = gemini.generate(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system=RESEARCH_SYSTEM, search=True, max_tokens=cfg.gemini_max_tokens)
    except GeminiError as exc:
        log.warning("Resers pasma %s selhala: %s", band, exc)
        return Dossier(text="(Resers pro toto pasmo selhala -- zadna data z webu.)",
                       ok=False)
    text = reply.text
    if len(text) > MAX_DOSSIER_CHARS:
        text = text[:MAX_DOSSIER_CHARS].rstrip() + "\n[...zkraceno]"
    log.info("Resers pasma %s: %s znaku, %s dotazu, %s zdroju.",
             band, len(reply.text), len(reply.queries), len(reply.sources))
    return Dossier(text=text, ok=True, sources=reply.sources, queries=reply.queries)


# --------------------------------------------------------------------------- #
# Prubeh debaty
# --------------------------------------------------------------------------- #
def opening_prompt(
    pool: dict[str, list[Tip]], dossiers: dict[str, Dossier], cfg: Config, now: datetime
) -> str:
    parts = [f"Today is {now.astimezone(cfg.tz):%A %d.%m.%Y}. The user will see the "
             f"best {cfg.tips_per_band} of each band."]
    for band, tips in pool.items():
        lo, hi = cfg.odds_bands[ord(band) - ord("A")]
        parts.append(f"\n=== BAND {band} (typical odds "
                     f"{band_range_text(lo, hi, decimal_comma=False)}) ===")
        parts += [describe(f"{band}{i}", t, cfg) for i, t in enumerate(tips, start=1)]
        parts.append(f"\nResearch dossier for band {band}:\n{dossiers[band].text}")
    parts.append("\nTASK: give your independent opening verdict for every band "
                 "above (reply_to_opponent = empty string).")
    return "\n".join(parts)


def _format_verdict(verdict: BandVerdict) -> str:
    lines = []
    for cid in verdict.order():
        r = verdict.ratings[cid]
        est = f"{r.safety} %" if r.safety is not None else "bez odhadu"
        lines.append(f"  {cid}: {est} - {r.argument}")
    return "\n".join(lines)


def turn_prompt(
    disputed: list[str],
    own: dict[str, BandVerdict],
    opponent: dict[str, BandVerdict],
    opponent_name: str,
    last: bool,
) -> str:
    parts = [f"{opponent_name} disagrees with you about the safest pick in "
             f"band(s) {', '.join(disputed)}. Their latest verdict:"]
    for band in disputed:
        opp = opponent[band]
        parts.append(f"\nBAND {band} - their pick {opp.pick()}, yours {own[band].pick()}.")
        parts.append(_format_verdict(opp))
        if opp.reply:
            parts.append(f"  Their reply to you: {opp.reply}")
    parts.append(
        "\nAnswer their strongest argument directly in reply_to_opponent "
        "(Czech, max ~300 characters) and give your updated ratings for ALL "
        f"candidates of band(s) {', '.join(disputed)} only.\n"
        "- Change your ranking only if they named a concrete fact or reasoning "
        "you missed or misjudged. Giving in just to reach agreement is a failure "
        "- and so is ignoring a good argument.\n"
        "- Point to specific facts from the dossier.")
    if last:
        parts.append("- This is the LAST exchange. If you still disagree, the "
                     "code decides by the average of both of your estimates.")
    return "\n".join(parts)


def skip_reason(
    pool: dict[str, list[Tip]], cfg: Config, injected: bool = False
) -> str | None:
    """Proc se AI krok nema spustit (None = ma bezet)."""
    if not cfg.ai_ranking:
        return "AI_RANKING=false"
    if not injected:
        if not cfg.anthropic_api_key:
            return "chybi ANTHROPIC_API_KEY"
        if not cfg.gemini_api_key:
            return "chybi GEMINI_API_KEY"
    if not any(len(tips) >= 2 for tips in pool.values()):
        return "v zadnem pasmu nejsou aspon 2 zapasy, neni co radit"
    return None


def _both(first, second):
    """Dva agenti odpovidaji zaroven (nezavisle uvodni verdikty)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut1, fut2 = ex.submit(first), ex.submit(second)
        return fut1.result(), fut2.result()


def run_debate(
    pool: dict[str, list[Tip]],
    cfg: Config,
    now: datetime,
    *,
    claude_client: Any | None = None,
    gemini_client: GeminiClient | None = None,
) -> AIRanking | None:
    """
    Resers + debata. Vraci None, kdyz je krok vypnuty, nema smysl, nebo selhal
    -- volajici pak pouzije kurzove poradi. Klienty predavaji jen testy.
    """
    injected = claude_client is not None and gemini_client is not None
    reason = skip_reason(pool, cfg, injected)
    if reason:
        log.info("AI debata preskocena: %s.", reason)
        return None

    debated = {band: tips for band, tips in pool.items() if len(tips) >= 2}
    band_ids = {band: [f"{band}{i}" for i in range(1, len(tips) + 1)]
                for band, tips in debated.items()}
    claude = gemini_agent = None
    try:
        gemini = gemini_client or GeminiClient(
            cfg.gemini_api_key, cfg.gemini_model, timeout=cfg.gemini_timeout)
        claude = ClaudeAgent(claude_client or make_anthropic_client(cfg), cfg)
        gemini_agent = GeminiAgent(gemini)

        # 1) resers -- pasma paralelne
        with ThreadPoolExecutor(max_workers=len(debated)) as ex:
            futures = {band: ex.submit(research_band, gemini, band, tips, cfg, now)
                       for band, tips in debated.items()}
            dossiers = {band: fut.result() for band, fut in futures.items()}

        # 2) uvodni verdikty -- nezavisle, zaroven
        prompt = opening_prompt(debated, dossiers, cfg, now)
        g_raw, c_raw = _both(lambda: gemini_agent.ask(prompt),
                             lambda: claude.ask(prompt))
        g_now, g_notes = parse_verdict(g_raw, band_ids)
        c_now, c_notes = parse_verdict(c_raw, band_ids)
        missing = [b for b in band_ids if b not in g_now or b not in c_now]
        if missing:
            raise AIRankingError(f"uvodni verdikt bez pasma {', '.join(missing)}")
        notes = [f"Gemini: {n}" for n in g_notes] + [f"Claude: {n}" for n in c_notes]

        log_rows: list[tuple[str, str, dict[str, BandVerdict]]] = [
            ("Gemini", "uvodni verdikt", {b: g_now[b] for b in band_ids}),
            ("Claude", "uvodni verdikt", {b: c_now[b] for b in band_ids}),
        ]
        agreed_at: dict[str, int] = {b: 0 for b in band_ids if g_now[b].pick() == c_now[b].pick()}

        # 3) debata -- stridave, jen o pasmech bez shody
        speakers = [(gemini_agent, g_now, c_now, "Claude"),
                    (claude, c_now, g_now, "Gemini")]
        for turn in range(1, cfg.ai_max_turns + 1):
            disputed = [b for b in band_ids if b not in agreed_at]
            if not disputed:
                break
            agent, own, opponent, opp_name = speakers[(turn - 1) % 2]
            raw = agent.ask(turn_prompt(disputed, own, opponent, opp_name,
                                        last=turn == cfg.ai_max_turns))
            update, turn_notes = parse_verdict(raw, {b: band_ids[b] for b in disputed})
            notes += [f"{agent.name} (odpoved {turn}): {n}" for n in turn_notes]
            own.update(update)      # pasmo, ktere agent vynechal, zustava beze zmeny
            log_rows.append((agent.name, f"odpoved {turn}", {b: own[b] for b in disputed}))
            for band in disputed:
                if g_now[band].pick() == c_now[band].pick():
                    agreed_at[band] = turn
                    log.info("AI debata: pasmo %s -- shoda na %s po %s. odpovedi.",
                             band, g_now[band].pick(), turn)
    except Exception as exc:  # noqa: BLE001 - jakekoli selhani = zpet ke kurzum
        log.warning("AI debata selhala, pouziji kurzove poradi: %s: %s",
                    type(exc).__name__, exc)
        return None

    ranking = _decide(pool, g_now, c_now, agreed_at, notes)
    ranking.model = f"{cfg.gemini_model} + {cfg.ai_model}"
    ranking.input_tokens = claude.input_tokens + claude.cache_write + claude.cache_read
    ranking.output_tokens = claude.output_tokens
    ranking.cost_usd = claude.cost_usd
    ranking.transcript = _transcript(pool, dossiers, log_rows, ranking, agreed_at, cfg, now)

    cost = f"~{ranking.cost_usd:.3f} USD" if ranking.cost_usd is not None else "cena neznama"
    log.info("AI debata hotova: Claude %s vstup (%s z cache) + %s vystup, %s; "
             "Gemini %s + %s tokenu (bezplatna uroven).",
             ranking.input_tokens, claude.cache_read, claude.output_tokens, cost,
             gemini_agent.input_tokens, gemini_agent.output_tokens)
    for note in notes:
        log.warning("AI debata: %s", note)
    return ranking


# --------------------------------------------------------------------------- #
# Vysledek pocita kod
# --------------------------------------------------------------------------- #
def _avg(*values: int | None) -> float:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else -1.0


def _decide(
    pool: dict[str, list[Tip]],
    g: dict[str, BandVerdict],
    c: dict[str, BandVerdict],
    agreed_at: dict[str, int],
    notes: list[str],
) -> AIRanking:
    order: dict[str, list[str]] = {}
    verdicts: dict[str, AIVerdict] = {}
    consensus: dict[str, bool] = {}
    for band, tips in pool.items():
        ids = [f"{band}{i}" for i in range(1, len(tips) + 1)]
        if band not in g:          # 0-1 kandidat: nebylo o cem debatovat
            order[band] = ids
            for rank, cid in enumerate(ids, start=1):
                verdicts[cid] = AIVerdict(rank=rank, reason="", confidence="")
            continue

        def score(cid: str) -> float:
            return _avg(g[band].ratings[cid].safety, c[band].ratings[cid].safety)

        ranked = sorted(ids, key=lambda cid: (-score(cid), int(cid[1:])))
        agreed = band in agreed_at
        if agreed:
            pick = g[band].pick()
            ranked.remove(pick)
            ranked.insert(0, pick)
        consensus[band] = agreed
        order[band] = ranked

        for rank, cid in enumerate(ranked, start=1):
            gr, cr = g[band].ratings[cid], c[band].ratings[cid]
            est = " · ".join(f"{name} {r.safety} %" for name, r in
                             (("G", gr), ("C", cr)) if r.safety is not None)
            # Skeptik ma posledni slovo: kdyz tip prosel jeho kritikou, je to duvod.
            argument = cr.argument or gr.argument
            reason = f"{est}: {argument}" if est and argument else (est or argument)
            if rank == 1:
                confidence = AGREED if agreed else DISPUTED
            else:
                confidence = THIN if THIN in (gr.evidence, cr.evidence) else SOLID
            verdicts[cid] = AIVerdict(rank=rank, reason=reason, confidence=confidence)
    return AIRanking(order=order, verdicts=verdicts, notes=notes, consensus=consensus)


def _transcript(
    pool: dict[str, list[Tip]],
    dossiers: dict[str, Dossier],
    rows: list[tuple[str, str, dict[str, BandVerdict]]],
    ranking: AIRanking,
    agreed_at: dict[str, int],
    cfg: Config,
    now: datetime,
) -> str:
    local = now.astimezone(cfg.tz)
    out = [f"# Debata {local:%d.%m.%Y}",
           "",
           f"Resers: {cfg.gemini_model} s vyhledavanim Google. "
           f"Analytik: {cfg.gemini_model}. Skeptik: {cfg.ai_model}. "
           f"Nejvys {cfg.ai_max_turns} odpovedi po uvodnich verdiktech."]
    for band, tips in pool.items():
        if band not in dossiers:
            continue
        out += ["", f"## Pasmo {band}", "", "| id | zapas | tip | trh |", "|---|---|---|---|"]
        out += [f"| {band}{i} | {match_name(t)} | {t.selection} | {t.prob * 100:.1f} % |"
                for i, t in enumerate(tips, start=1)]
        d = dossiers[band]
        out += ["", "### Resers", "", d.text]
        if d.sources:
            out += ["", "Zdroje: " + ", ".join(f"[{title}]({uri})" for title, uri in d.sources[:15])]
        for who, label, verdicts in rows:
            if band not in verdicts:
                continue
            v = verdicts[band]
            out += ["", f"### {who} -- {label} (1. misto: {v.pick()})"]
            if v.reply:
                out += ["", f"> {v.reply}"]
            out += [""] + [f"- **{cid}** {v.ratings[cid].safety if v.ratings[cid].safety is not None else '?'} %"
                           f" -- {v.ratings[cid].argument}" for cid in v.order()]
        top = ranking.order[band][0]
        if band in agreed_at:
            result = (f"shoda na {top} "
                      + ("hned v uvodnich verdiktech" if agreed_at[band] == 0
                         else f"po {agreed_at[band]}. odpovedi"))
        else:
            result = f"BEZ SHODY -- rozhodl prumer obou odhadu: {top}"
        out += ["", f"**Vysledek:** {result}. Poradi: {', '.join(ranking.order[band])}."]
    return "\n".join(out) + "\n"
