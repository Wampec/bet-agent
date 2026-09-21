"""
state.py -- denik tipu a statistiky po kurzovych pasmech.

Uklada se kazdy vybrany tip: ty zobrazene ve zprave (pasma A, B, ...) i tichy
zbytek mimo pasma (pod / nad), ktery se neposila, ale sbira se kvuli
pozdejsimu porovnani. Zadny bankroll ani vyse sazek -- penize si uzivatel
ridi sam. Virtualni zisk ve statistikach je jen spolecne meritko pasem.

Soubor je jednoduchy JSON (STATE_FILE, vychozi ./tips.json). Zaznamy se
nemazou, protoze statistiky pasem davaji smysl az po mesicich.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

STATUS_OPEN = "open"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_VOID = "void"

# Po jak dlouhe dobe od zacatku ma smysl ptat se na vysledek. Tenis, basketbal
# i hazena se do 4 hodin vejdou; drivejsi dotaz by jen platil kredity za
# jeste probihajici zapas.
FINISHED_AFTER_HOURS = 4.0
# Jak dlouho zpetne se jeste zkousi dohledat vysledek. Endpoint /scores
# neposkytuje starsi nez 3 dny.
GIVE_UP_AFTER_HOURS = 72.0


@dataclass
class SentTip:
    """Jeden ulozeny tip."""

    sent_at: str
    event_id: str
    sport_key: str
    sport_title: str
    commence_time: str
    home_team: str
    away_team: str
    market: str
    selection: str
    pick_team: str
    prob: float
    odds: float                       # nejlepsi nalezeny kurz
    status: str = STATUS_OPEN
    settled_at: str | None = None
    result_note: str | None = None
    median_odds: float | None = None  # bezny kurz; u starsich zaznamu chybi
    band: str | None = None           # A, B, ... nebo pod / nad / mezi
    shown: bool = True                # False = tichy zbytek, neposlal se
    n_outcomes: int | None = None     # 3 = trh s remizou; u starsich zaznamu chybi
    # Poradi v ramci pasma: podle sance (odds_rank) a podle AI (ai_rank).
    # AI poradi je None u tipu z doby pred AI krokem nebo kdyz AI nebezela.
    odds_rank: int | None = None
    ai_rank: int | None = None
    ai_reason: str | None = None
    ai_confidence: str | None = None

    @property
    def commence_dt(self) -> datetime:
        return datetime.fromisoformat(self.commence_time)

    @property
    def has_draw_market(self) -> bool:
        """Tricestny trh: remiza je prohrana sazka, ne vraceny vklad."""
        if self.n_outcomes is not None:
            return self.n_outcomes >= 3
        # Starsi zaznamy typ trhu nemaji -- z podporovanych sportu ma
        # tricestny trh jen hazena.
        return self.sport_key.startswith("handball")

    @property
    def typical_odds(self) -> float:
        return self.median_odds or self.odds

    def is_finished(self, now: datetime) -> bool:
        hours = (now - self.commence_dt).total_seconds() / 3600.0
        return hours >= FINISHED_AFTER_HOURS

    def is_too_old(self, now: datetime) -> bool:
        hours = (now - self.commence_dt).total_seconds() / 3600.0
        return hours > GIVE_UP_AFTER_HOURS


@dataclass
class BandStats:
    """Souhrn jednoho pasma."""

    wins: int = 0
    decided: int = 0          # vyhry + prohry; void se nepocita
    pending: int = 0
    expected_sum: float = 0.0  # soucet predpovezenych sanci u rozhodnutych tipu
    profit: float = 0.0        # virtualni zisk pri stejne sazce na kazdy tip

    @property
    def rate(self) -> float | None:
        return self.wins / self.decided if self.decided else None

    @property
    def expected(self) -> float | None:
        """Jakou uspesnost agent predpovidal -- kdyz sedi se skutecnosti, vypocty funguji."""
        return self.expected_sum / self.decided if self.decided else None


@dataclass
class AIComparison:
    """(vyhry, rozhodnute) pro AI vyber a pro cisty kurzovy vyber."""

    ai_top: tuple[int, int] = (0, 0)
    odds_top: tuple[int, int] = (0, 0)
    ai_only: tuple[int, int] = (0, 0)     # AI je vybrala, kurzy ne
    odds_only: tuple[int, int] = (0, 0)   # kurzy je vybraly, AI je vyradila

    @property
    def has_data(self) -> bool:
        return self.ai_top[1] > 0 or self.odds_top[1] > 0


@dataclass
class TipLog:
    path: Path
    tips: list[SentTip] = field(default_factory=list)
    last_sent_day: str | None = None

    # -- serializace -------------------------------------------------------- #
    @classmethod
    def load(cls, path: Path) -> "TipLog":
        if not path.exists():
            log.info("Denik tipu zatim neexistuje, zakladam novy.")
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Denik '%s' nelze precist (%s) -- zakladam novy.", path, exc)
            return cls(path=path)

        known = set(SentTip.__dataclass_fields__)
        return cls(
            path=path,
            tips=[
                SentTip(**{k: v for k, v in item.items() if k in known})
                for item in raw.get("tips", [])
            ],
            last_sent_day=raw.get("last_sent_day"),
        )

    def save(self) -> None:
        payload: dict[str, Any] = {
            "updated_at": _now_iso(),
            "last_sent_day": self.last_sent_day,
            "tips": [asdict(t) for t in self.tips],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)
        log.debug("Denik ulozen do %s (%s zaznamu)", self.path, len(self.tips))

    # -- prace se zaznamy --------------------------------------------------- #
    def add(self, tips: list[SentTip], local_day: str) -> None:
        known = {(t.event_id, t.market): t for t in self.tips}
        for tip in tips:
            existing = known.get((tip.event_id, tip.market))
            if existing is None:
                self.tips.append(tip)
                known[(tip.event_id, tip.market)] = tip
            elif existing.median_odds is None and tip.median_odds is not None:
                # Zaznam z doby pred pasmy mel jen nejlepsi kurz a pasmo dostal
                # podle nej. Kdyz se zapas objevi znovu, doplni se bezny kurz
                # a pasmo se opravi. Uz odeslany tip zustava zobrazeny.
                existing.median_odds = tip.median_odds
                existing.band = tip.band
                existing.n_outcomes = tip.n_outcomes
                existing.shown = existing.shown or tip.shown
        self.last_sent_day = local_day

    def already_sent_today(self, local_day: str) -> bool:
        return self.last_sent_day == local_day

    def reassign_bands(self, band_for_odds: Callable[[float], str]) -> int:
        """
        Prepocita pasmo u vsech zaznamu podle aktualne nastavenych pasem.

        Pasmo je odvozene jen z kurzu, takze po zmene ODDS_BANDS se cela
        historie roztridi znovu a statistiky odpovidaji novym pasmum. U starsich
        zaznamu bez bezneho kurzu se pouzije ulozeny nejlepsi kurz.
        Vraci pocet zaznamu, kterym se pasmo zmenilo.
        """
        changed = 0
        for tip in self.tips:
            band = band_for_odds(tip.typical_odds)
            if tip.band != band:
                tip.band = band
                changed += 1
        return changed

    def pending(self, now: datetime | None = None) -> list[SentTip]:
        """Tipy, u kterych uz zapas skoncil, ale vysledek jeste nezname."""
        now = now or datetime.now(timezone.utc)
        return [
            t for t in self.tips
            if t.status == STATUS_OPEN and t.is_finished(now) and not t.is_too_old(now)
        ]

    def expire_old(self, now: datetime | None = None) -> int:
        """Zaznamy, u kterych uz vysledek nedohledame, oznaci jako void."""
        now = now or datetime.now(timezone.utc)
        count = 0
        for tip in self.tips:
            if tip.status == STATUS_OPEN and tip.is_too_old(now):
                tip.status = STATUS_VOID
                tip.result_note = "vysledek se nepodarilo dohledat"
                tip.settled_at = _now_iso()
                count += 1
        return count

    def record_result(self, tip: SentTip, status: str, note: str) -> None:
        tip.status = status
        tip.result_note = note
        tip.settled_at = _now_iso()

    def recently_settled(self, hours: float = 36.0,
                         shown_only: bool = True) -> list[SentTip]:
        """Tipy vyhodnocene v poslednich hodinach -- podklad pro rekapitulaci."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = [
            t for t in self.tips
            if t.status != STATUS_OPEN and t.settled_at
            and _safe_dt(t.settled_at) >= cutoff
            and (t.shown or not shown_only)
        ]
        out.sort(key=lambda t: t.commence_time)
        return out

    def band_stats(self, band: str, stake: float,
                   shown_only: bool = True) -> BandStats:
        """
        Statistika pasma. Vychozi je jen to, co uzivatel opravdu videl ve
        zprave; tichy zaznam (napr. tip, ktery AI vyradila) by jinak zkresloval
        cisla, ktera ma uzivatel pred ocima.
        """
        stats = BandStats()
        for tip in self.tips:
            if tip.band != band:
                continue
            if shown_only and not tip.shown:
                continue
            if tip.status == STATUS_OPEN:
                stats.pending += 1
            elif tip.status == STATUS_WON:
                stats.wins += 1
                stats.decided += 1
                stats.expected_sum += tip.prob
                stats.profit += stake * (tip.typical_odds - 1.0)
            elif tip.status == STATUS_LOST:
                stats.decided += 1
                stats.expected_sum += tip.prob
                stats.profit -= stake
        return stats

    def ai_comparison(self, top_n: int) -> "AIComparison":
        """
        Porovna, jak dopadl vyber AI a jak by dopadl cisty vyber podle sance.

        Bere jen tipy z dnu, kdy AI bezela (maji ai_rank). V kazdem pasmu jsou
        ulozene oba vybery -- AI top_n (poslalo se) i kurzove top_n (kdyz se
        lisi, ulozilo se potichu) -- takze je srovnani ferove.
        """
        settled = [t for t in self.tips
                   if t.ai_rank is not None and t.status in (STATUS_WON, STATUS_LOST)]
        ai_top = [t for t in settled if t.ai_rank <= top_n]
        odds_top = [t for t in settled
                    if t.odds_rank is not None and t.odds_rank <= top_n]
        ai_only = [t for t in ai_top if not (t.odds_rank is not None and t.odds_rank <= top_n)]
        odds_only = [t for t in odds_top if not t.ai_rank <= top_n]

        def rate(items: list[SentTip]) -> tuple[int, int]:
            return sum(1 for t in items if t.status == STATUS_WON), len(items)

        return AIComparison(
            ai_top=rate(ai_top), odds_top=rate(odds_top),
            ai_only=rate(ai_only), odds_only=rate(odds_only),
        )

    def record(self, shown_only: bool = True) -> tuple[int, int]:
        """(pocet vyher, pocet rozhodnutych tipu) -- void se nepocita."""
        decided = [t for t in self.tips
                   if t.status in (STATUS_WON, STATUS_LOST) and (t.shown or not shown_only)]
        return sum(1 for t in decided if t.status == STATUS_WON), len(decided)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_dt(raw: str | None) -> datetime:
    try:
        return datetime.fromisoformat(raw) if raw else datetime.min.replace(
            tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Vyhodnoceni podle vysledku ze /scores
# --------------------------------------------------------------------------- #
def evaluate_score(tip: SentTip, payload: dict[str, Any]) -> tuple[str, str] | None:
    """
    Z odpovedi endpointu /scores urci vysledek tipu.
    Vraci (status, popis) nebo None, kdyz zapas jeste neni ukonceny.
    """
    if not payload.get("completed"):
        return None

    scores = {
        item.get("name"): _to_float(item.get("score"))
        for item in payload.get("scores") or []
    }
    home = scores.get(tip.home_team)
    away = scores.get(tip.away_team)
    if home is None or away is None:
        return None

    detail = f"{tip.home_team} {home:g}:{away:g} {tip.away_team}"

    if tip.market == "double_chance":
        status = STATUS_WON if home >= away else STATUS_LOST
    elif tip.market == "draw_no_bet":
        if home > away:
            status = STATUS_WON
        elif home == away:
            status = STATUS_VOID
        else:
            status = STATUS_LOST
    elif tip.market == "h2h":
        picked = scores.get(tip.pick_team)
        others = [v for k, v in scores.items() if k != tip.pick_team]
        if picked is None or not others:
            return None
        best_other = max(others)
        if picked > best_other:
            status = STATUS_WON
        elif picked < best_other:
            status = STATUS_LOST
        elif tip.has_draw_market:
            # Hazena: tip zni "favorit vyhraje", takze remiza je prohra.
            status = STATUS_LOST
        else:
            # Dvoucestny trh bez viteze (nedohrano) -- kancelare sazku rusi.
            status = STATUS_VOID
    else:
        return None

    return status, detail


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
