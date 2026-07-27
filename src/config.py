"""Central config + env loading. No secrets hard-coded."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DB = DATA_DIR / "cache.db"


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

MONKS_API_KEY = os.environ.get("MONKS_API_KEY", "")
SPORTMONKS_BASE = "https://api.sportmonks.com/v3/football"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
REASONING_PROVIDER = os.environ.get("REASONING_PROVIDER", "anthropic")
REASONING_MODEL = os.environ.get("REASONING_MODEL", "claude-opus-4-8")

# Kalshi (execution venue) — read endpoints are public, trading needs RSA key
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID") or os.environ.get("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = (os.environ.get("KALSHI_PRIVATE_KEY_PATH")
                           or str(ROOT / "kalshi_private_key.txt"))

# Kalshi World Cup series tickers (discovered via probing)
KALSHI_SERIES = {
    "winner": "KXMENWORLDCUP",     # outright champion (one market per nation)
    "match": "KXWCGAME",           # per-match 3-way: TEAM / TIE / TEAM
    "h2h": "KXWCTEAMH2H",          # team head-to-head
    "group_winner": "KXWCGROUPWIN",
    "reach_round": "KXWCROUND",
    "spread": "KXWCSPREAD",
    "total": "KXWCTOTAL",
    "team_total": "KXWCTEAMTOTAL",
    "btts": "KXWCBTTS",
    "golden_boot": "KXWCGOALLEADER",
}

# SportMonks IDs discovered via probing
WC_LEAGUE_ID = 732
WC_SEASON_ID = 26618
QUALIFIER_SEASON_IDS = {
    "CAF": 22005,        # Africa
    # other confederation qualifier season ids resolved at runtime via league currentSeason
}
QUALIFIER_LEAGUE_IDS = [711, 714, 717, 720, 723, 726, 729]

# Bankroll / staking — conservative until CLV proves the model out
BANKROLL_USD = 250.0
KELLY_FRACTION = 0.25             # quarter-Kelly until the ledger shows +CLV
MAX_STAKE_FRACTION = 0.12         # per-bet hard cap as a fraction of bankroll
MIN_EDGE = 0.06                   # anchored bets: >=6% edge (fees eat ~40% of 4%)
MIN_EDGE_UNANCHORED = 0.09        # no sharp-line anchor: demand more edge
SHARP_GATE_MARGIN = 0.01          # sharp line must agree by >=1 point
MAX_SPREAD_CENTS = 12             # reject illiquid books (no real two-sided price)
MAX_PORTFOLIO_FRACTION = 0.50     # at most 50% of bankroll deployed at once
MAX_GROUP_FRACTION = 0.15         # at most 15% on one correlated group (team/match)

# Kill-switch: stop placing if the model is demonstrably behind the close
KILL_SWITCH_MIN_BETS = 30         # evaluate after this many settled placed bets
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
