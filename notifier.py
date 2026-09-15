"""
notifier.py -- formatovani a odeslani zpravy pres Telegram Bot API.

Pouziva parse_mode=MarkdownV2, takze vsechen dynamicky text musi projit
funkci esc() -- Telegram jinak zpravu odmitne s chybou 400.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests

from analyzer import AnalysisResult, Tip, band_key, band_range_text
from config import Config
from state import STATUS_LOST, STATUS_VOID, STATUS_WON, BandStats, SentTip

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4096
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"

SPORT_ICONS = {
    "Tennis": "\U0001f3be",
    "Basketball": "\U0001f3c0",
    "Handball": "\U0001f93e",
    "Baseball": "⚾",
    "Ice Hockey": "\U0001f3d2",
    "Soccer": "⚽",
}
BAND_ICONS = ("\U0001f7e2", "\U0001f7e0", "\U0001f535", "\U0001f7e3")
RESULT_ICONS = {STATUS_WON: "✅", STATUS_LOST: "❌", STATUS_VOID: "↩️"}


class TelegramError(RuntimeError):
    """Chyba pri odesilani zpravy na Telegram."""


def esc(text: object) -> str:
    """Escapuje text pro MarkdownV2."""
    out = str(text)
    for char in _ESCAPE_CHARS:
        out = out.replace(char, f"\\{char}")
    return out


def _pct(value: float, decimals: int = 1) -> str:
    """Procenta s desetinnou carkou, uz escapovana."""
    return esc(f"{value * 100:.{decimals}f}".replace(".", ",")) + " %"


def _num(value: float, decimals: int = 2) -> str:
    return esc(f"{value:.{decimals}f}".replace(".", ","))


def _money(value: float, currency: str = "Kc") -> str:
    sign = "+" if value > 0 else ""
    return esc(f"{sign}{value:.0f} {currency}")


def _when(moment: datetime, cfg: Config, now: datetime | None = None) -> str:
    """Cas zacatku slovy: 'dnes 19:40' / 'zitra 02:30'."""
    local = moment.astimezone(cfg.tz)
    now_local = (now or datetime.now(cfg.tz)).astimezone(cfg.tz)
    delta_days = (local.date() - now_local.date()).days
    prefix = {0: "dnes", 1: "zitra"}.get(delta_days)
    if prefix is None:
        return local.strftime("%d.%m. %H:%M")
    return f"{prefix} {local.strftime('%H:%M')}"


def band_icon(key: str) -> str:
    index = ord(key) - ord("A") if len(key) == 1 else -1
    return BAND_ICONS[index] if 0 <= index < len(BAND_ICONS) else "\U0001f539"


# --------------------------------------------------------------------------- #
# Klient
# --------------------------------------------------------------------------- #
class TelegramNotifier:
    def __init__(self, cfg: Config, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self._session = requests.Session()

    def send(self, text: str, parse_mode: str = "MarkdownV2") -> None:
        for chunk in _split(text):
            self._send_chunk(chunk, parse_mode)

    def _send_chunk(self, text: str, parse_mode: str) -> None:
        if self.dry_run:
            log.info("[DRY RUN] Telegram zprava:\n%s", text)
            return

        url = f"{TELEGRAM_API}/bot{self.cfg.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        last_error = ""
        for attempt in range(1, self.cfg.http_retries + 1):
            try:
                resp = self._session.post(
                    url, json=payload, timeout=self.cfg.http_timeout
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 200:
                log.info("Telegram zprava odeslana (%s znaku).", len(text))
                return

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code == 400 and parse_mode:
                # Nejcasteji spatny escape -- zkusime jeste jednou bez formatovani.
                log.warning("Telegram odmitl formatovani (%s), posilam jako text.",
                            last_error)
                payload["parse_mode"] = ""
                payload["text"] = _strip_md(text)
                parse_mode = ""
                continue
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(int(retry_after), 30))
                continue
            time.sleep(min(2 ** attempt, 8))

        raise TelegramError(f"Zpravu se nepodarilo odeslat: {last_error}")


def _split(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE_LEN:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def _strip_md(text: str) -> str:
    out = text
    for char in _ESCAPE_CHARS:
        out = out.replace(f"\\{char}", char)
    return out.replace("*", "").replace("_", "")


# --------------------------------------------------------------------------- #
# Formatovani zprav
# --------------------------------------------------------------------------- #
def _band_title(index: int, cfg: Config) -> str:
    key = band_key(index)
    lo, hi = cfg.odds_bands[index]
    return (f"{band_icon(key)} *PASMO {esc(key)}* · "
            f"kurz {esc(band_range_text(lo, hi))}")


def _recap_block(settled: list[SentTip], cfg: Config) -> list[str]:
    """Jak dopadly naposledy vyhodnocene zobrazene tipy, po pasmech."""
    lines: list[str] = []
    for index in range(len(cfg.odds_bands)):
        key = band_key(index)
        in_band = [t for t in settled if t.band == key]
        if not in_band:
            continue
        parts = [f"{RESULT_ICONS.get(t.status, '•')} {esc(t.selection)}" for t in in_band]
        lines.append(f"{band_icon(key)} {esc(key)}: " + " · ".join(parts))
    if lines:
        lines.insert(0, "\U0001f4cb *Minule tipy*")
    return lines


def _tip_lines(position: int, tip: Tip, cfg: Config, now: datetime) -> list[str]:
    odds = f"kurz {_num(tip.typical_odds)}"
    if round(tip.odds, 2) > round(tip.typical_odds, 2):
        odds += f" \\(max {_num(tip.odds)}\\)"
    first = f"*{esc(position)}\\. {esc(tip.selection)}* — {_pct(tip.prob)} · {odds}"

    icon = SPORT_ICONS.get(tip.event.sport_group, "\U0001f3c5")
    details = [esc(tip.event.sport_title), esc(_when(tip.event.commence_time, cfg, now))]
    if tip.opponent:
        details.append(f"proti {esc(tip.opponent)}")
    if tip.has_draw:
        details.append("remiza \\= prohra")
    elif tip.market != "h2h":
        details.append(esc(tip.market_label))
    return [first, f"    {icon} " + " · ".join(details)]


def _stats_line(stats: BandStats, cfg: Config) -> str:
    if not stats.decided:
        waiting = f" \\(ceka {esc(stats.pending)}\\)" if stats.pending else ""
        return f"\U0001f4ca zatim bez vyhodnocenych tipu{waiting}"
    return (
        f"\U0001f4ca {esc(stats.wins)}/{esc(stats.decided)} \\({_pct(stats.rate, 0)}\\)"
        f" · cekano {_pct(stats.expected, 0)}"
        f" · virtualne {_money(stats.profit)}"
    )


def format_tips(
    result: AnalysisResult,
    selected: dict[str, list[Tip]],
    stats: dict[str, BandStats],
    settled: list[SentTip],
    cfg: Config,
    now: datetime | None = None,
) -> str:
    """Denni zprava: rekapitulace + tipy po kurzovych pasmech."""
    now = now or datetime.now(cfg.tz)
    lines = [f"\U0001f3af *TIPY NA {esc(now.astimezone(cfg.tz).strftime('%d.%m.'))}*"]

    recap = _recap_block(settled, cfg)
    if recap:
        lines += [""] + recap

    for index in range(len(cfg.odds_bands)):
        key = band_key(index)
        tips = selected.get(key, [])
        lines += ["", _band_title(index, cfg)]
        if not tips:
            lines.append("    dnes nic v tomto pasmu")
        for position, tip in enumerate(tips, start=1):
            lines += _tip_lines(position, tip, cfg, now)
        if len(tips) > 1:
            combined = 1.0
            for tip in tips:
                combined *= tip.prob
            lines.append(f"\U0001f9ee Vsechny {esc(len(tips))} najednou: {_pct(combined)}")
        lines.append(_stats_line(stats.get(key, BandStats()), cfg))

    if not any(selected.get(band_key(i)) for i in range(len(cfg.odds_bands))):
        lines += ["", f"Prohledano zapasu: *{esc(result.considered)}*"]
        if result.funnel:
            lines.append(f"Duvody vyrazeni: {esc(result.funnel_line())}")

    lines += ["", esc(f"Virtualne = zisk pri {cfg.virtual_stake:.0f} Kc "
                      "na kazdy tip, jen pro srovnani pasem.")]
    return "\n".join(lines)


def format_quota_warning(remaining: int, cfg: Config) -> str:
    """Kvota The Odds API dochazi -- agent radeji nestahuje kurzy."""
    return "\n".join([
        "\U0001f6d1 *Dochazi kvota The Odds API*",
        "",
        f"Zbyva *{esc(remaining)}* kreditu, rezerva je "
        f"{esc(cfg.min_credits_reserve)}\\.",
        esc("Dnes uz kurzy nestahuji. Kvota se obnovuje na zacatku "
            "dalsiho mesice; do te doby muzes snizit MAX_SPORT_KEYS."),
    ])
