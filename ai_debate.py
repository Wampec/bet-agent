"""
ai_debate.py -- dva AI agenti se v kazdem pasmu dohaduji, ktery zapas je
nejtutovejsi.

Postup
------
1. RESERS: GPT s vyhledavanim na webu pripravi ke kazdemu zapasu zvlast
   rozbor pred zapasem (forma, sestavy, H2H, styl, motivace, program, povrch
   nebo pocasi, co cekaji preview). Vyhledavani je vynucene -- z pameti
   (zastarale) se nepise; rozbor bez vyhledavani se zahodi.
   Stejne podklady dostanou oba agenti.
2. UVODNI VERDIKTY: GPT (ANALYTIK) a Claude (SKEPTIK) nezavisle na sobe
   ohodnoti kazdeho kandidata: vlastni odhad sance na vyhru (0-100) + argument.
3. DEBATA: shoda = stejne 1. misto a odhady nejvys AI_AGREE_GAP bodu od sebe,
   takze se posuzuje i pasmo s jedinym zapasem. Kde shoda neni, stridaji se
   v odpovedich (GPT, Claude, GPT...) -- kazdy vidi posledni verdikt
   a argumenty soupere -- dokud se neshodnou, nejvys AI_MAX_TURNS odpovedi.
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
    AGREED, DISPUTED, SOLID, SPORT_CS, THIN,
    AIRanking, AIRankingError, AIVerdict,
    describe, estimate_cost, make_anthropic_client, match_name,
)
from analyzer import Tip, band_range_text
from config import Config
from gpt import GPTClient, GPTError

log = logging.getLogger(__name__)

MAX_MATCH_CHARS = 4500        # rozbor jednoho zapasu -- drzi cenu vstupu na uzde
RESEARCH_WORKERS = 4          # soubezne reserse
RESEARCH_ATTEMPTS = 2         # kolikrat zkusit rozbor, kdyz model na webu nehleda

# --------------------------------------------------------------------------- #
# Zadani
# --------------------------------------------------------------------------- #
RESEARCH_SYSTEM = """\
You are an experienced sports analyst preparing pre-match briefings for a \
bettor. Use web search thoroughly - several searches per match (form, \
injuries and team news, head-to-head, previews and predictions). ALWAYS \
search: your own knowledge is out of date - new teams, leagues and players \
appear every season, so never claim a team or player does not exist; search \
for it instead. Be concrete: \
dates, scores, names, numbers. Never invent anything; say plainly what you \
could not find. Write in Czech.\
"""

RESEARCH_TASK = """\
Today is {today}. Prepare a pre-match briefing:
{sport} | {competition} | {match} | starts {when} (Prague time)
The market favourite is {pick}{draw}; bookmakers give it about {prob} %.

Cover as much as you can find:
1. Current form: last 5-10 results of both sides with dates and scores, the \
trend and the quality of the opponents they faced.
2. Team news: injuries, suspensions, illness, rest or rotation, expected \
line-ups; in tennis fitness, recent retirements or withdrawals.
3. Head-to-head: recent meetings, scores, and on this surface or venue.
4. Style match-up: how both sides play and why that helps or hurts the \
favourite (pace, defence, rebounding, serve and return, surface fit...).
5. Context: standings and motivation, what is at stake, schedule and rest \
days, travel, back-to-back games, home and away records this season.
6. Conditions: surface, indoor or outdoor, altitude; weather for outdoor events.
7. What previews, experts and tipsters expect (name the source) and how the \
odds have moved, if you find it.
8. Summary: the 2-3 strongest reasons the favourite wins and the 2-3 biggest \
risks of an upset.

Mark anything uncertain or older than a few weeks. 250-450 words.\
"""

SEARCH_NUDGE = """

IMPORTANT: your previous answer did not use web search at all. Search the \
web now (several searches) - answers from memory are out of date and useless \
here. Use only what you find.\
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
You are the ANALYST (agent GPT). You build the case from data: recent form, \
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


class GPTAgent:
    name = "GPT"

    def __init__(self, client: GPTClient, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self.system = f"{ANALYST_ROLE}\n\n{COMMON_RULES}"
        self.messages: list[dict] = []
        self.input_tokens = self.output_tokens = 0

    def ask(self, prompt: str) -> Any:
        self.messages.append({"role": "user", "content": prompt})
        try:
            reply = self.client.generate(
                self.messages, system=self.system, schema=VERDICT_SCHEMA,
                max_tokens=self.cfg.openai_max_tokens, effort=self.cfg.ai_effort,
                model=self.cfg.openai_model)
        except GPTError as exc:
            raise AIRankingError(f"GPT: {exc}") from exc
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.messages.append({"role": "assistant", "content": reply.text})
        try:
            return json.loads(reply.text)
        except json.JSONDecodeError as exc:
            raise AIRankingError(f"GPT: odpoved neni platny JSON: {exc}") from exc


# --------------------------------------------------------------------------- #
# Resers -- kazdy zapas zvlast, jako rozbor pred zapasem
# --------------------------------------------------------------------------- #
@dataclass
class Dossier:
    text: str
    ok: bool
    sources: list[tuple[str, str]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


def research_match(
    gpt: GPTClient, cid: str, tip: Tip, cfg: Config, now: datetime
) -> Dossier:
    ev = tip.event
    prompt = RESEARCH_TASK.format(
        today=f"{now.astimezone(cfg.tz):%A %d.%m.%Y}",
        sport=SPORT_CS.get(ev.sport_group, ev.sport_group or ev.sport_key),
        competition=ev.sport_title,
        match=match_name(tip),
        when=f"{ev.commence_time.astimezone(cfg.tz):%d.%m. %H:%M}",
        pick=tip.pick_team,
        prob=f"{tip.prob * 100:.0f}",
        draw=" (a draw would lose the tip)" if tip.has_draw else "",
    )
    # Model se muze rozhodnout nehledat a psat z pameti -- ta je zastarala
    # (nezna treba nove tymy) a agenty by svedla. Proto se to zkousi znovu
    # s duraznym pokynem; kdyz nehleda ani pak, rozbor se zahodi.
    reply = None
    try:
        for attempt in range(1, RESEARCH_ATTEMPTS + 1):
            text = prompt if attempt == 1 else prompt + SEARCH_NUDGE
            reply = gpt.generate(
                [{"role": "user", "content": text}],
                system=RESEARCH_SYSTEM, search=True, max_searches=cfg.openai_max_searches,
                max_tokens=cfg.openai_max_tokens, effort="low",
                model=cfg.openai_research_model)
            if reply.queries:
                break
            log.warning("Resers %s: GPT nehledal na webu (pokus %s/%s).",
                        cid, attempt, RESEARCH_ATTEMPTS)
    except GPTError as exc:
        log.warning("Resers %s (%s) selhala: %s", cid, match_name(tip), exc)
        return Dossier(text="(Resers k tomuto zapasu selhala -- zadna data z webu.)",
                       ok=False)
    if reply is None or not reply.queries:
        log.warning("Resers %s (%s): GPT na webu nehledal, rozbor zahazuji.",
                    cid, match_name(tip))
        return Dossier(text="(GPT k tomuto zapasu na webu nic nevyhledal -- "
                            "zadna data z webu.)", ok=False)
    text = reply.text
    if len(text) > MAX_MATCH_CHARS:
        text = text[:MAX_MATCH_CHARS].rstrip() + "\n[...zkraceno]"
    log.info("Resers %s (%s): %s znaku, %s dotazu, %s zdroju.",
             cid, match_name(tip), len(reply.text), len(reply.queries), len(reply.sources))
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
        for i, tip in enumerate(tips, start=1):
            cid = f"{band}{i}"
            parts.append("\n" + describe(cid, tip, cfg))
            parts.append(f"Research dossier for {cid}:\n{dossiers[cid].text}")
    parts.append("\nTASK: give your independent opening verdict for every band "
                 "above (reply_to_opponent = empty string). A band with a single "
                 "candidate still needs your honest estimate and argument.")
    return "\n".join(parts)


def _format_verdict(verdict: BandVerdict) -> str:
    lines = []
    for cid in verdict.order():
        r = verdict.ratings[cid]
        est = f"{r.safety} %" if r.safety is not None else "bez odhadu"
        lines.append(f"  {cid}: {est} - {r.argument}")
    return "\n".join(lines)


def _pick_text(verdict: BandVerdict) -> str:
    pick = verdict.pick()
    safety = verdict.ratings[pick].safety
    return f"{pick} ({safety} %)" if safety is not None else pick


def agreed(g: BandVerdict, c: BandVerdict, gap: int) -> bool:
    """Shoda = stejne 1. misto A odhady jeho sance se lisi nejvys o `gap` bodu."""
    if g.pick() != c.pick():
        return False
    gs, cs = g.ratings[g.pick()].safety, c.ratings[c.pick()].safety
    return gs is not None and cs is not None and abs(gs - cs) <= gap


def turn_prompt(
    disputed: list[str],
    own: dict[str, BandVerdict],
    opponent: dict[str, BandVerdict],
    opponent_name: str,
    gap: int,
    last: bool,
) -> str:
    parts = [f"{opponent_name} disagrees with you in band(s) {', '.join(disputed)} - "
             "either about the safest pick or about how safe it is (your estimates "
             f"for the pick differ by more than {gap} points). Their latest verdict:"]
    for band in disputed:
        opp = opponent[band]
        parts.append(f"\nBAND {band} - their pick {_pick_text(opp)}, "
                     f"yours {_pick_text(own[band])}.")
        parts.append(_format_verdict(opp))
        if opp.reply:
            parts.append(f"  Their reply to you: {opp.reply}")
    parts.append(
        "\nAnswer their strongest argument directly in reply_to_opponent "
        "(Czech, max ~300 characters) and give your updated ratings for ALL "
        f"candidates of band(s) {', '.join(disputed)} only.\n"
        "- Change your ranking or estimate only if they named a concrete fact or "
        "reasoning you missed or misjudged. Giving in just to reach agreement is "
        "a failure - and so is ignoring a good argument.\n"
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
        if not cfg.openai_api_key:
            return "chybi OPENAI_API_KEY"
    if not any(pool.values()):
        return "v pasmech neni zadny zapas"
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
    gpt_client: GPTClient | None = None,
) -> AIRanking | None:
    """
    Resers + debata. Vraci None, kdyz je krok vypnuty, nema smysl, nebo selhal
    -- volajici pak pouzije kurzove poradi. Klienty predavaji jen testy.
    """
    injected = claude_client is not None and gpt_client is not None
    reason = skip_reason(pool, cfg, injected)
    if reason:
        log.info("AI debata preskocena: %s.", reason)
        return None

    debated = {band: tips for band, tips in pool.items() if tips}
    band_ids = {band: [f"{band}{i}" for i in range(1, len(tips) + 1)]
                for band, tips in debated.items()}
    gap = cfg.ai_agree_gap
    claude = gpt_agent = gpt = None
    try:
        gpt = gpt_client or GPTClient(
            cfg.openai_api_key, cfg.openai_model, timeout=cfg.openai_timeout)
        claude = ClaudeAgent(claude_client or make_anthropic_client(cfg), cfg)
        gpt_agent = GPTAgent(gpt, cfg)

        # 1) resers -- kazdy zapas zvlast
        matches = {f"{band}{i}": tip for band, tips in debated.items()
                   for i, tip in enumerate(tips, start=1)}
        with ThreadPoolExecutor(max_workers=RESEARCH_WORKERS) as ex:
            futures = {cid: ex.submit(research_match, gpt, cid, tip, cfg, now)
                       for cid, tip in matches.items()}
            dossiers = {cid: fut.result() for cid, fut in futures.items()}

        # 2) uvodni verdikty -- nezavisle, zaroven
        prompt = opening_prompt(debated, dossiers, cfg, now)
        g_raw, c_raw = _both(lambda: gpt_agent.ask(prompt),
                             lambda: claude.ask(prompt))
        g_now, g_notes = parse_verdict(g_raw, band_ids)
        c_now, c_notes = parse_verdict(c_raw, band_ids)
        missing = [b for b in band_ids if b not in g_now or b not in c_now]
        if missing:
            raise AIRankingError(f"uvodni verdikt bez pasma {', '.join(missing)}")
        notes = [f"GPT: {n}" for n in g_notes] + [f"Claude: {n}" for n in c_notes]

        log_rows: list[tuple[str, str, dict[str, BandVerdict]]] = [
            ("GPT", "uvodni verdikt", {b: g_now[b] for b in band_ids}),
            ("Claude", "uvodni verdikt", {b: c_now[b] for b in band_ids}),
        ]
        agreed_at: dict[str, int] = {
            b: 0 for b in band_ids if agreed(g_now[b], c_now[b], gap)}

        # 3) debata -- stridave, jen o pasmech bez shody
        speakers = [(gpt_agent, g_now, c_now, "Claude"),
                    (claude, c_now, g_now, "GPT")]
        for turn in range(1, cfg.ai_max_turns + 1):
            disputed = [b for b in band_ids if b not in agreed_at]
            if not disputed:
                break
            agent, own, opponent, opp_name = speakers[(turn - 1) % 2]
            raw = agent.ask(turn_prompt(disputed, own, opponent, opp_name, gap,
                                        last=turn == cfg.ai_max_turns))
            update, turn_notes = parse_verdict(raw, {b: band_ids[b] for b in disputed})
            notes += [f"{agent.name} (odpoved {turn}): {n}" for n in turn_notes]
            own.update(update)      # pasmo, ktere agent vynechal, zustava beze zmeny
            log_rows.append((agent.name, f"odpoved {turn}", {b: own[b] for b in disputed}))
            for band in disputed:
                if agreed(g_now[band], c_now[band], gap):
                    agreed_at[band] = turn
                    log.info("AI debata: pasmo %s -- shoda na %s po %s. odpovedi.",
                             band, g_now[band].pick(), turn)
    except Exception as exc:  # noqa: BLE001 - jakekoli selhani = zpet ke kurzum
        log.warning("AI debata selhala, pouziji kurzove poradi: %s: %s",
                    type(exc).__name__, exc)
        return None

    ranking = _decide(pool, g_now, c_now, agreed_at, notes)
    ranking.model = f"{cfg.openai_research_model} / {cfg.openai_model} + {cfg.ai_model}"
    ranking.input_tokens = claude.input_tokens + claude.cache_write + claude.cache_read
    ranking.output_tokens = claude.output_tokens
    gpt_cost = getattr(gpt, "cost_usd", 0.0) if getattr(gpt, "cost_known", True) else None
    ranking.cost_usd = (claude.cost_usd + gpt_cost
                        if claude.cost_usd is not None and gpt_cost is not None else None)
    ranking.transcript = _transcript(pool, dossiers, log_rows, ranking, agreed_at, cfg, now)

    def usd(value: float | None) -> str:
        return f"~{value:.3f} USD" if value is not None else "cena neznama"

    log.info("AI debata hotova: Claude %s vstup (%s z cache) + %s vystup, %s; "
             "GPT %s hledani, analytik %s + %s tokenu, %s; celkem %s.",
             ranking.input_tokens, claude.cache_read, claude.output_tokens,
             usd(claude.cost_usd), getattr(gpt, "searches", 0),
             gpt_agent.input_tokens, gpt_agent.output_tokens, usd(gpt_cost),
             usd(ranking.cost_usd))
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
        if band not in g:          # prazdne pasmo
            order[band] = ids
            continue

        def score(cid: str) -> float:
            return _avg(g[band].ratings[cid].safety, c[band].ratings[cid].safety)

        ranked = sorted(ids, key=lambda cid: (-score(cid), int(cid[1:])))
        is_agreed = band in agreed_at
        if is_agreed:
            pick = g[band].pick()
            ranked.remove(pick)
            ranked.insert(0, pick)
        consensus[band] = is_agreed
        order[band] = ranked

        for rank, cid in enumerate(ranked, start=1):
            gr, cr = g[band].ratings[cid], c[band].ratings[cid]
            est = " · ".join(f"{name} {r.safety} %" for name, r in
                             (("GPT", gr), ("Claude", cr)) if r.safety is not None)
            # Skeptik ma posledni slovo: kdyz tip prosel jeho kritikou, je to duvod.
            argument = cr.argument or gr.argument
            reason = f"{est}: {argument}" if est and argument else (est or argument)
            if rank == 1:
                confidence = AGREED if is_agreed else DISPUTED
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
           f"Resers: {cfg.openai_research_model} s vyhledavanim na webu, kazdy zapas zvlast. "
           f"Analytik: {cfg.openai_model}. Skeptik: {cfg.ai_model}. "
           f"Shoda = stejne 1. misto a odhady nejvys {cfg.ai_agree_gap} bodu od sebe. "
           f"Nejvys {cfg.ai_max_turns} odpovedi po uvodnich verdiktech."]
    for band, tips in pool.items():
        if band not in ranking.consensus:
            continue
        out += ["", f"## Pasmo {band}", "", "| id | zapas | tip | trh |", "|---|---|---|---|"]
        out += [f"| {band}{i} | {match_name(t)} | {t.selection} | {t.prob * 100:.1f} % |"
                for i, t in enumerate(tips, start=1)]
        for i, tip in enumerate(tips, start=1):
            cid = f"{band}{i}"
            d = dossiers[cid]
            out += ["", f"### Resers {cid}: {match_name(tip)}", "", d.text]
            if d.sources:
                out += ["", "Zdroje: " + ", ".join(
                    f"[{title}]({uri})" for title, uri in d.sources[:10])]
        for who, label, verdicts in rows:
            if band not in verdicts:
                continue
            v = verdicts[band]
            out += ["", f"### {who} -- {label} (1. misto: {_pick_text(v)})"]
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
