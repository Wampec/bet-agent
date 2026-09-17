"""
main.py -- denni beh agenta.

Postup:
  1. dohleda vysledky drive odeslanych tipu (pro rekapitulaci),
  2. stahne dnesni nabidku vybranych sportu (den i nadchazejici noc),
  3. spocita sanci na vyhru favorita a seradi zapasy od nejjistejsiho,
  4. zaradi je do kurzovych pasem (ODDS_BANDS) a v kazdem posle na Telegram
     TIPS_PER_BAND nejjistejsich; zapasy mimo pasma ulozi potichu.

Agent neresi vysi sazky ani bankroll -- jen doporucuje zapasy.

Priklady spusteni:
    python main.py                # bezny denni beh
    python main.py --dry-run      # nic neposila ani neuklada, jen vypise
    python main.py --no-telegram  # ulozi denik, zpravu vypise do konzole
    python main.py --status       # prehled deniku tipu
    python main.py --results-only # jen dohleda vysledky, nove tipy neposila
    python main.py -v             # podrobny log

Na GitHubu (viz .github/workflows/denni-tipy.yml) bezi:
    python main.py --once-per-day --not-before 07:55
Spousti ho externe cron-job.org v 8:00 a zalozne planovac GitHubu, proto
agent sam hlida, aby zprava prisla jednou denne a ne drive nez rano v Praze.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, time, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from analyzer import (
    REST_ABOVE,
    REST_BELOW,
    Tip,
    analyze,
    band_for_odds,
    band_key,
    band_range_text,
    prefilter_soccer_events,
    select_by_band,
)
from config import Config, ConfigError, enable_utf8_stdout, load_config
from fetcher import (
    Event,
    OddsAPIClient,
    OddsAPIError,
    enrich_soccer_markets,
    fetch_daily_events,
)
from notifier import (
    TelegramError,
    TelegramNotifier,
    format_quota_warning,
    format_tips,
)
from state import STATUS_OPEN, SentTip, TipLog, evaluate_score

log = logging.getLogger("bet-agent")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_API = 3


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Denni tipy na zapasy s nejvyssi sanci na vyhru favorita."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="nic neodesila na Telegram ani neuklada denik")
    parser.add_argument("--no-telegram", action="store_true",
                        help="denik ulozi, ale zpravu jen vypise do konzole")
    parser.add_argument("--status", action="store_true",
                        help="vypise prehled deniku tipu a skonci")
    parser.add_argument("--results-only", action="store_true",
                        help="jen dohleda vysledky, nove tipy neposila")
    parser.add_argument("--once-per-day", action="store_true",
                        help="nic nedelat, pokud uz dnes zprava odesla "
                             "(pro opakovane spousteni z planovace)")
    parser.add_argument("--not-before", type=_parse_hhmm, metavar="HH:MM",
                        help="nic nedelat pred timto mistnim casem (TIMEZONE)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="podrobny log (DEBUG)")
    return parser.parse_args(argv)


def _parse_hhmm(raw: str) -> time:
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"cas musi byt ve tvaru HH:MM, ne '{raw}'") from exc


def skip_reason(
    now_local: datetime,
    tips: TipLog,
    *,
    once_per_day: bool,
    not_before: time | None,
) -> str | None:
    """
    Duvod, proc dnesni beh preskocit, nebo None.

    Kontroluje se jeste pred jakymkoli dotazem na API, takze preskoceny beh
    nestoji zadne kredity.
    """
    if not_before is not None and now_local.time() < not_before:
        return (f"je {now_local:%H:%M}, zprava se posila az od "
                f"{not_before:%H:%M}")
    if once_per_day and tips.already_sent_today(now_local.strftime("%Y-%m-%d")):
        return "dnesni zprava uz byla odeslana"
    return None


def setup_logging(level: str, log_file: Path | None = None) -> None:
    enable_utf8_stdout()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        # Pri spusteni z planovace uloh neni videt konzole, takze bez souboroveho
        # logu by po nocnim behu nezbyla zadna stopa.
        try:
            handlers.append(
                RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3,
                                    encoding="utf-8")
            )
        except OSError as exc:  # napr. read-only adresar
            print(f"Log do souboru se nepodarilo otevrit: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def print_status(tips: TipLog, cfg: Config) -> None:
    print(f"Denik:        {tips.path}")
    print(f"Zaznamu:      {len(tips.tips)} (ceka na vysledek {len(tips.pending())})")
    print(f"Sporty:       {', '.join(cfg.sport_groups) or '-'}"
          + (" + fotbal" if cfg.enable_soccer else ""))
    print(f"Rozpocet API: {cfg.describe_budget()}")

    rows = [(band_key(i), band_range_text(lo, hi, decimal_comma=False), True)
            for i, (lo, hi) in enumerate(cfg.odds_bands)]
    rows.append((REST_BELOW, f"pod {cfg.odds_bands[0][0]:.2f}", False))
    if not math.isinf(cfg.odds_bands[-1][1]):
        rows.append((REST_ABOVE, f"nad {cfg.odds_bands[-1][1]:.2f}", False))
    print(f"\nVirtualni zisk pri {cfg.virtual_stake:.0f} Kc na tip:")
    print(f"  {'pasmo':6} {'kurz':12} {'uspesnost':>14} {'cekano':>7} {'zisk':>9} {'ceka':>5}")
    for key, label, shown in rows:
        s = tips.band_stats(key, cfg.virtual_stake)
        rate = f"{s.wins}/{s.decided} ({s.rate:.0%})" if s.decided else "-"
        expected = f"{s.expected:.0%}" if s.decided else "-"
        tag = "" if shown else "  (tichy zbytek)"
        print(f"  {key:6} {label:12} {rate:>14} {expected:>7} "
              f"{s.profit:+8.0f}  {s.pending:>4}{tag}")

    recent = tips.recently_settled(hours=24 * 14)
    if recent:
        print("\nPoslednich 14 dni (zobrazene tipy):")
        for tip in recent[-15:]:
            print(f"  [{tip.band or '?'}] {tip.status:5}  {tip.selection[:28]:28}  "
                  f"{tip.prob * 100:5.1f} %  kurz {tip.typical_odds:.2f}  "
                  f"{tip.result_note or ''}")


# --------------------------------------------------------------------------- #
# Dohledani vysledku
# --------------------------------------------------------------------------- #
def settle_results(client: OddsAPIClient, tips: TipLog) -> int:
    """
    Dohleda vysledky u tipu, jejichz zapas uz skoncil.
    Dotazy se seskupuji po soutezich, protoze kazdy stoji kredity.
    """
    # Tipy, u kterych uz vysledek API neposkytne (starsi nez 3 dny), se uzavrou
    # vzdy -- i kdyz zrovna nic jineho na vysledek neceka.
    expired = tips.expire_old()
    if expired:
        log.info("%s tipu bez dohledatelneho vysledku oznaceno jako neplatne.",
                 expired)

    pending = tips.pending()
    if not pending:
        return 0

    by_sport: dict[str, list[SentTip]] = {}
    for tip in pending:
        by_sport.setdefault(tip.sport_key, []).append(tip)
    log.info("Dohledavam vysledky u %s tipu (%s souteci).",
             len(pending), len(by_sport))

    settled = 0
    for sport_key, group in by_sport.items():
        try:
            scores = client.fetch_scores(sport_key, days_from=3)
        except OddsAPIError as exc:
            log.error("Vysledky pro '%s' se nepodarilo stahnout: %s", sport_key, exc)
            continue
        index = {item.get("id"): item for item in scores}
        for tip in group:
            payload = index.get(tip.event_id)
            if payload is None:
                continue
            outcome = evaluate_score(tip, payload)
            if outcome is None:
                continue
            status, note = outcome
            tips.record_result(tip, status, note)
            log.info("  %s: %s (%s)", tip.selection, status, note)
            settled += 1
    return settled


def _notify(notifier: TelegramNotifier, text: str) -> None:
    try:
        notifier.send(text)
    except TelegramError as exc:
        log.error("Telegram: %s", exc)


# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Chyba konfigurace: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    setup_logging("DEBUG" if args.verbose else cfg.log_level,
                  cfg.state_file.parent / "agent.log")

    tips = TipLog.load(cfg.state_file)

    if not args.status:
        reason = skip_reason(datetime.now(cfg.tz), tips,
                             once_per_day=args.once_per_day,
                             not_before=args.not_before)
        if reason:
            log.info("Beh preskocen: %s.", reason)
            return EXIT_OK

    # Pasmo je odvozene z kurzu -- po zmene ODDS_BANDS se historie roztridi znovu.
    changed = tips.reassign_bands(lambda odds: band_for_odds(odds, cfg.odds_bands))
    if changed:
        log.info("Podle aktualnich pasem prerazeno %s tipu.", changed)
        if not args.dry_run:
            tips.save()

    if args.status:
        print_status(tips, cfg)
        return EXIT_OK

    offline = args.dry_run or args.no_telegram
    try:
        cfg.validate(require_telegram=not offline)
    except ConfigError as exc:
        log.error("Chyba konfigurace: %s", exc)
        print(f"Chyba konfigurace: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    notifier = TelegramNotifier(cfg, dry_run=offline)
    client = OddsAPIClient(cfg)

    # --- 1. vysledky drivejsich tipu --------------------------------------- #
    if cfg.track_results:
        try:
            settle_results(client, tips)
        except OddsAPIError as exc:
            log.error("The Odds API: %s", exc)
        if not args.dry_run:
            tips.save()
    if args.results_only:
        return EXIT_OK

    # --- 2. kontrola kvoty (vypis souteci je zdarma) ------------------------ #
    try:
        client.list_sports()
    except OddsAPIError as exc:
        log.error("The Odds API: %s", exc)
        return EXIT_API
    remaining = client.quota.remaining
    if remaining is not None and remaining < cfg.min_credits_reserve:
        log.warning("Zbyva jen %s kreditu (rezerva %s) -- kurzy dnes nestahuji.",
                    remaining, cfg.min_credits_reserve)
        _notify(notifier, format_quota_warning(remaining, cfg))
        return EXIT_OK

    # --- 3. dnesni nabidka -------------------------------------------------- #
    try:
        events = fetch_daily_events(client)
    except OddsAPIError as exc:
        log.error("The Odds API: %s", exc)
        return EXIT_API

    pool: list[Event] = [e for e in events if not e.is_soccer]
    soccer = [e for e in events if e.is_soccer]
    if cfg.enable_soccer and soccer:
        shortlist = prefilter_soccer_events(soccer, cfg)
        try:
            enrich_soccer_markets(client, shortlist)
        except OddsAPIError as exc:
            log.error("Doplnkove trhy: %s", exc)
        pool += shortlist[: cfg.max_event_market_lookups]

    # --- 4. vyber tipu ------------------------------------------------------ #
    now = datetime.now(timezone.utc)
    result = analyze(pool, cfg, now)
    log.info("Spotreba API: %s", client.quota)

    # V kazdem pasmu nejvyse TIPS_PER_BAND tipu s nejvyssi sanci. Zapasy mimo
    # pasma (pod / nad) se neposilaji, ale ukladaji se kvuli porovnani.
    selected = select_by_band(result.tips, cfg)
    shown_keys = {band_key(i) for i in range(len(cfg.odds_bands))}
    local_day = now.astimezone(cfg.tz).strftime("%Y-%m-%d")

    if not args.dry_run:
        tips.add(
            [
                to_sent_tip(tip, band=key, shown=key in shown_keys, now=now)
                for key, picked in selected.items()
                for tip in picked
            ],
            local_day,
        )
        tips.save()

    message = format_tips(
        result,
        selected,
        stats={key: tips.band_stats(key, cfg.virtual_stake) for key in shown_keys},
        settled=tips.recently_settled(),
        cfg=cfg,
        now=now,
    )
    _notify(notifier, message)
    return EXIT_OK


def to_sent_tip(tip: Tip, *, band: str, shown: bool, now: datetime) -> SentTip:
    return SentTip(
        sent_at=now.isoformat(timespec="seconds"),
        event_id=tip.event.event_id,
        sport_key=tip.event.sport_key,
        sport_title=tip.event.sport_title,
        commence_time=tip.event.commence_time.isoformat(),
        home_team=tip.event.home_team,
        away_team=tip.event.away_team,
        market=tip.market,
        selection=tip.selection,
        pick_team=tip.pick_team,
        prob=round(tip.prob, 4),
        odds=tip.odds,
        status=STATUS_OPEN,
        median_odds=round(tip.typical_odds, 3),
        band=band,
        shown=shown,
        n_outcomes=tip.n_outcomes,
    )


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print("Preruseno uzivatelem.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
