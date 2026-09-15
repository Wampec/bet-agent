"""
config.py -- centralni konfigurace agenta.

Odkud se hodnoty berou (vyssi ma prednost):
  1. promenne prostredi -- na GitHubu z Actions secrets,
  2. .env               -- jen tajne klice, lokalne; nikdy se necommituje,
  3. settings.env       -- bezne nastaveni (pasma, sporty...), je v repozitari
                           a da se upravit primo na GitHubu,
  4. vychozi hodnoty v tomhle souboru.
Zadny API klic nikdy nepatri primo do kodu ani do settings.env.

K CEMU AGENT SLOUZI
-------------------
Kazdy den posle na Telegram nekolik zapasu s nejvyssi sanci na vyhru favorita.
Neresi vysi sazky, bankroll ani kurzove okno -- to si uzivatel ridi sam
v aplikaci sazkove kancelare. Jedinym kriteriem je pravdepodobnost a
spolehlivost jejiho odhadu (kolik kancelari ji potvrzuje).
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Soubory se nacitaji jen pokud existuji a zadny neprepisuje hodnotu, ktera uz
# je nastavena -- proto poradi urcuje prednost.
load_dotenv(BASE_DIR / ".env", override=False)
load_dotenv(BASE_DIR / "settings.env", override=False)


class ConfigError(RuntimeError):
    """Chybejici nebo nevalidni konfigurace."""


def enable_utf8_stdout() -> None:
    """
    Windows konzole ma vychozi kodovani cp1250 a emoji ve zpravach by shodily
    vypis vyjimkou UnicodeEncodeError. Prepneme stdout/stderr na UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# --------------------------------------------------------------------------- #
# Sportovni profily
# --------------------------------------------------------------------------- #
# Nazvy skupin tak, jak je vraci The Odds API v poli "group".
#
# Tenis a basketbal maji dvoucestny trh (bez remizy), takze "vyhra favorita"
# je primo jeden ze dvou vysledku a odhad pravdepodobnosti je nejcistsi.
# Hazena ma trojcestny trh -- remiza je tam mozna, byva kolem 3-8 %, a tip
# "vyhra favorita" ji ma jako prohranou sazku. Spoctena sance to zohlednuje.
# Baseball a hokej jsou v profilu "wide"; vyrazne favority maji vzacneji.
SPORT_PROFILES: dict[str, tuple[str, ...]] = {
    "individual": ("Tennis",),
    "low_variance": ("Tennis", "Basketball"),
    "core": ("Tennis", "Basketball", "Handball"),
    "wide": ("Tennis", "Basketball", "Baseball", "Ice Hockey", "Handball"),
}
DEFAULT_SPORT_PROFILE = "core"

# Fotbal potrebuje odvozene trhy (1X / DNB), ktere se dotahuji po jednotlivych
# zapasech = 2 kredity za zapas. Proto je ve vychozim nastaveni vypnuty.
DEFAULT_SOCCER_LEAGUES: tuple[str, ...] = (
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
)

TENNIS_TOP_TIER_KEYWORDS: tuple[str, ...] = (
    "aus_open", "french_open", "wimbledon", "us_open",
    "indian_wells", "miami_open", "monte_carlo", "madrid_open",
    "italian_open", "rome_masters", "canadian_open", "cincinnati",
    "shanghai_masters", "paris_masters", "china_open", "wuhan_open",
    "qatar_open", "dubai",
)


# --------------------------------------------------------------------------- #
# Pomocne prevody
# --------------------------------------------------------------------------- #
def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _float(key: str, default: float) -> float:
    raw = _str(key)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{key}: nelze prevest '{raw}' na cislo") from exc


def _int(key: str, default: int) -> int:
    raw = _str(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}: nelze prevest '{raw}' na cele cislo") from exc


def _bool(key: str, default: bool) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "ano"}


def _csv(key: str, default: str = "") -> list[str]:
    raw = _str(key) or default
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Konfiguracni objekt
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    # --- API klice -------------------------------------------------------- #
    odds_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    # --- The Odds API ----------------------------------------------------- #
    odds_api_base: str = "https://api.the-odds-api.com/v4"
    # Kazdy region navic nasobi spotrebu kreditu. Jeden region ("eu") dava
    # bezne 12-23 kancelari, coz na spolehlivy odhad bohate staci.
    regions: list[str] = field(default_factory=lambda: ["eu"])
    odds_format: str = "decimal"
    http_timeout: int = 20
    http_retries: int = 3

    # Kolik souteci se maximalne dotazuje. Souteze bez zapasu jsou zdarma,
    # takze realna spotreba odpovida poctu souteci, ktere zrovna hraji.
    max_sport_keys: int = 12
    # Kdyz na uctu zbyva min kreditu, agent uz nestahuje kurzy a posle varovani.
    min_credits_reserve: int = 40
    # Kolik fotbalovych zapasu se doptava na trhy 1X/DNB (2 kredity za zapas).
    max_event_market_lookups: int = 0

    # --- Vyber sportu ----------------------------------------------------- #
    sport_profile: str = DEFAULT_SPORT_PROFILE
    sport_groups: list[str] = field(
        default_factory=lambda: list(SPORT_PROFILES[DEFAULT_SPORT_PROFILE])
    )
    tennis_top_tier_only: bool = False
    tennis_tier_keywords: list[str] = field(
        default_factory=lambda: list(TENNIS_TOP_TIER_KEYWORDS)
    )
    sport_key_whitelist: list[str] = field(default_factory=list)
    sport_key_blacklist: list[str] = field(default_factory=list)

    enable_soccer: bool = False
    soccer_home_favourite_only: bool = True
    soccer_markets: list[str] = field(
        default_factory=lambda: ["double_chance", "draw_no_bet"]
    )
    soccer_league_whitelist: list[str] = field(
        default_factory=lambda: list(DEFAULT_SOCCER_LEAGUES)
    )

    # --- Casove okno ------------------------------------------------------ #
    timezone: str = "Europe/Prague"
    min_minutes_before_start: int = 45
    # 24 h = cely dnesni den i nadchazejici noc. Nocni zapasy jsou podstatne:
    # NBA zacina v evropskem case typicky mezi 01:00 a 05:00 nasledujiciho dne.
    max_hours_ahead: int = 24
    same_day_only: bool = False

    # --- Vyber tipu ------------------------------------------------------- #
    # Kurzova pasma. Zapas se zaradi podle bezneho (medianoveho) kurzu
    # favorita zaokrouhleneho na setiny -- u ceske kancelare, ktera v datech
    # neni, dostanes spis kurz kolem medianu nez ten nejlepsi. V kazdem pasmu
    # se posle TIPS_PER_BAND zapasu s nejvyssi sanci.
    #   A 1,15-1,19: nejvyssi jistota tipu, k 2x je potreba 4-5 vyher
    #   B 1,20-1,24: k 2x je potreba 4 vyhry
    #   C 1,25 a vys: k 2x staci 3-4 vyhry (bez horni hranice)
    # Zapasy mimo pasma se ukladaji potichu (neposilaji se), aby slo pozdeji
    # porovnat, jak by si vedly.
    odds_bands: tuple[tuple[float, float], ...] = (
        (1.15, 1.19), (1.20, 1.24), (1.25, math.inf))
    tips_per_band: int = 3
    # Sazka pro virtualni zisk ve statistikach. Neni to doporuceni vkladu,
    # jen spolecne meritko, aby slo pasma ferove porovnat.
    virtual_stake: float = 100.0
    # Tvrdy prah -- zapasy pod nim se vubec nenabizeji. 0 = bez prahu.
    min_tip_prob: float = 0.0

    # --- Spolehlivost odhadu ---------------------------------------------- #
    # Tohle nejsou filtry na "kvalitu sazky", ale na to, jestli se da odhadu
    # pravdepodobnosti verit: kolik kancelari ho tvori a jak moc se rozchazeji.
    min_bookmakers: int = 4
    max_prob_dispersion: float = 0.04
    devig_method: str = "shin"           # shin | multiplicative | power
    # True = brat jen sporty bez remizy (vyradi hazenou i hokej v zakl. dobe).
    # Nemichani dvoucestnych a trojcestnych nabidek v ramci jednoho zapasu
    # resi agent sam -- vzdy pouzije jen prevazujici strukturu trhu.
    two_way_only: bool = False
    max_market_divergence: float = 0.05  # jen pro fotbalove odvozene trhy

    # --- Provoz ----------------------------------------------------------- #
    track_results: bool = True           # hlasit, jak dopadly predchozi tipy
    state_file: Path = BASE_DIR / "tips.json"
    log_level: str = "INFO"

    # ---------------------------------------------------------------- #
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def validate(self, *, require_telegram: bool = True) -> None:
        problems: list[str] = []
        if not self.odds_api_key:
            problems.append("chybi ODDS_API_KEY")
        if require_telegram and not self.telegram_bot_token:
            problems.append("chybi TELEGRAM_BOT_TOKEN")
        if require_telegram and not self.telegram_chat_id:
            problems.append("chybi TELEGRAM_CHAT_ID")
        if self.tips_per_band < 1:
            problems.append("TIPS_PER_BAND musi byt >= 1")
        if not 0.0 <= self.min_tip_prob < 1.0:
            problems.append("MIN_TIP_PROB musi byt v intervalu [0, 1)")
        if self.virtual_stake <= 0:
            problems.append("VIRTUAL_STAKE musi byt > 0")
        problems += _band_problems(self.odds_bands)
        if self.devig_method not in {"shin", "multiplicative", "power"}:
            problems.append("DEVIG_METHOD musi byt shin | multiplicative | power")
        if self.max_sport_keys < 1:
            problems.append("MAX_SPORT_KEYS musi byt >= 1")
        if self.min_bookmakers < 2:
            problems.append("MIN_BOOKMAKERS musi byt >= 2")
        if not self.sport_groups and not self.enable_soccer:
            problems.append("neni vybran zadny sport (SPORT_PROFILE / ENABLE_SOCCER)")
        try:
            _ = self.tz
        except Exception:  # noqa: BLE001 - typicky chybejici tzdata na Windows
            problems.append(
                f"neznama casova zona '{self.timezone}' "
                "(na Windows nainstaluj balicek tzdata)"
            )
        if problems:
            raise ConfigError("Nevalidni konfigurace: " + "; ".join(problems))

    def describe_budget(self) -> str:
        """Odhad denni spotreby kreditu The Odds API."""
        regions = len(self.regions)
        keys = self.max_sport_keys * regions
        soccer = 0
        if self.enable_soccer:
            soccer = (
                len(self.soccer_league_whitelist) * regions
                + self.max_event_market_lookups * len(self.soccer_markets) * regions
            )
        # Vysledky se stahuji po soutezich (2 kredity za soutez), ne po tipech.
        results = 2 * len(self.sport_groups) if self.track_results else 0
        worst = keys + soccer + results
        return (
            f"nejvyse ~{worst} kreditu/den (souteze {keys}"
            + (f" + fotbal {soccer}" if soccer else "")
            + (f" + vysledky {results}" if results else "")
            + f"), tedy do ~{worst * 30}/mesic pri limitu free tier 500. "
            "Souteze, ktere zrovna nehraji, jsou zdarma, takze skutecna "
            "spotreba byva vyrazne nizsi."
        )


# --------------------------------------------------------------------------- #
def _sport_groups(profile: str) -> list[str]:
    """SPORT_GROUPS ma prednost pred SPORT_PROFILE."""
    explicit = _csv("SPORT_GROUPS")
    if explicit:
        return explicit
    if profile not in SPORT_PROFILES:
        raise ConfigError(
            f"SPORT_PROFILE '{profile}' neexistuje "
            f"(dostupne: {', '.join(sorted(SPORT_PROFILES))})"
        )
    return list(SPORT_PROFILES[profile])


DEFAULT_ODDS_BANDS = "1.15-1.19,1.20-1.24,1.25+"


def _odds_bands() -> tuple[tuple[float, float], ...]:
    """
    ODDS_BANDS='1.15-1.19,1.20-1.24,1.25+'
        -> ((1.15, 1.19), (1.20, 1.24), (1.25, inf))

    'od+' znamena pasmo bez horni hranice; smi byt jen posledni.
    """
    raw = _str("ODDS_BANDS") or DEFAULT_ODDS_BANDS
    bands: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if part.endswith("+"):
                lo, hi = float(part[:-1].strip()), math.inf
            else:
                lo, hi = (float(x.strip()) for x in part.split("-"))
        except ValueError as exc:
            raise ConfigError(
                f"ODDS_BANDS: '{part}' neni ve tvaru od-do (1.15-1.19) "
                "ani od+ (1.25+)"
            ) from exc
        bands.append((lo, hi))
    return tuple(bands)


def _band_problems(bands: tuple[tuple[float, float], ...]) -> list[str]:
    if not bands:
        return ["ODDS_BANDS musi obsahovat aspon jedno pasmo"]
    problems = []
    for lo, hi in bands:
        if lo <= 1.0 or hi < lo:
            problems.append(f"ODDS_BANDS: neplatne pasmo {lo}-{hi}")
    for (_, prev_hi), (lo, _) in zip(bands, bands[1:]):
        if lo <= prev_hi:
            problems.append("ODDS_BANDS: pasma musi byt vzestupne a neprekryvat se")
            break
    return problems


def _soccer_leagues() -> list[str]:
    """SOCCER_LEAGUE_WHITELIST: prazdne = default seznam, 'ALL' = vsechny ligy."""
    raw = _str("SOCCER_LEAGUE_WHITELIST")
    if not raw:
        return list(DEFAULT_SOCCER_LEAGUES)
    if raw.upper() == "ALL":
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_config() -> Config:
    """Sestavi Config z promennych prostredi / .env."""
    state_file = _str("STATE_FILE")
    profile = _str("SPORT_PROFILE", DEFAULT_SPORT_PROFILE).lower()
    enable_soccer = _bool("ENABLE_SOCCER", False)
    return Config(
        odds_api_key=_str("ODDS_API_KEY"),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        odds_api_base=_str("ODDS_API_BASE", "https://api.the-odds-api.com/v4"),
        regions=_csv("ODDS_REGIONS", "eu"),
        http_timeout=_int("HTTP_TIMEOUT", 20),
        http_retries=_int("HTTP_RETRIES", 3),
        max_sport_keys=_int("MAX_SPORT_KEYS", 12),
        min_credits_reserve=_int("MIN_CREDITS_RESERVE", 40),
        max_event_market_lookups=_int("MAX_EVENT_MARKET_LOOKUPS",
                                      4 if enable_soccer else 0),
        sport_profile=profile,
        sport_groups=_sport_groups(profile),
        tennis_top_tier_only=_bool("TENNIS_TOP_TIER_ONLY", False),
        sport_key_whitelist=_csv("SPORT_KEY_WHITELIST"),
        sport_key_blacklist=_csv("SPORT_KEY_BLACKLIST"),
        enable_soccer=enable_soccer,
        soccer_home_favourite_only=_bool("SOCCER_HOME_FAVOURITE_ONLY", True),
        soccer_markets=_csv("SOCCER_MARKETS", "double_chance,draw_no_bet"),
        soccer_league_whitelist=_soccer_leagues(),
        timezone=_str("TIMEZONE", "Europe/Prague"),
        min_minutes_before_start=_int("MIN_MINUTES_BEFORE_START", 45),
        max_hours_ahead=_int("MAX_HOURS_AHEAD", 24),
        same_day_only=_bool("SAME_DAY_ONLY", False),
        odds_bands=_odds_bands(),
        tips_per_band=_int("TIPS_PER_BAND", 3),
        virtual_stake=_float("VIRTUAL_STAKE", 100.0),
        min_tip_prob=_float("MIN_TIP_PROB", 0.0),
        min_bookmakers=_int("MIN_BOOKMAKERS", 4),
        max_prob_dispersion=_float("MAX_PROB_DISPERSION", 0.04),
        devig_method=_str("DEVIG_METHOD", "shin").lower(),
        two_way_only=_bool("TWO_WAY_ONLY", False),
        max_market_divergence=_float("MAX_MARKET_DIVERGENCE", 0.05),
        track_results=_bool("TRACK_RESULTS", True),
        state_file=Path(state_file) if state_file else BASE_DIR / "tips.json",
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )
