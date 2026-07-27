"""Fetch live Kalshi books for one match across all its markets.

Builds {market_key: {"ticker":…, "yes_ask":…, "no_ask":…, ...}} (cents) for
match_markets.py. Prices come from each market's ORDER BOOK — the list/summary
fields are null for these markets. Both sides are returned so the edge engine
can evaluate YES and NO.
"""
from . import config
from .kalshi import KalshiReadClient
from .teams import same_team, team_in_text


def _has(sub: str, name: str) -> bool:
    return team_in_text(sub, name)


def _entity_books(series_key: str, k: KalshiReadClient | None = None) -> dict:
    """Per-entity futures series: {sub_title: {"ticker":…, **book}}."""
    k = k or KalshiReadClient()
    out = {}
    for m in k.markets(series_ticker=config.KALSHI_SERIES[series_key], status="open"):
        sub, ticker = m.get("yes_sub_title"), m.get("ticker")
        if not (sub and ticker):
            continue
        book = k.book_prices(ticker)
        if book:
            out[sub] = {"ticker": ticker, **book}
    return out


def get_winner_books(k=None) -> dict:
    return _entity_books("winner", k)


def get_golden_boot_books(k=None) -> dict:
    return _entity_books("golden_boot", k)


def get_match_prices(home: str, away: str, k: KalshiReadClient | None = None) -> dict:
    k = k or KalshiReadClient()
    prices: dict = {}

    def add(key, m):
        tk = m.get("ticker")
        book = k.book_prices(tk) if tk else {}
        if book:
            prices[key] = {"ticker": tk, **book}

    # 1X2 from KXWCGAME — match the game by parsing the two teams from the title
    code = None
    game_date = ""  # ISO date of kickoff (from occurrence_datetime)
    for m in k.markets(series_ticker=config.KALSHI_SERIES["match"], status="open"):
        tt = (m.get("title") or "").replace(" Winner?", "").split(" vs ")
        if len(tt) != 2:
            continue
        a, b = tt[0].strip(), tt[1].strip()
        if not ((same_team(a, home) and same_team(b, away)) or
                (same_team(a, away) and same_team(b, home))):
            continue
        tk = m.get("ticker")
        if tk and code is None and len(tk.split("-")) > 1:
            code = tk.split("-")[1]
        if not game_date:
            game_date = (m.get("occurrence_datetime") or "")[:10]
        sub = m.get("yes_sub_title") or ""
        if "tie" in sub.lower() or "draw" in sub.lower():
            add("draw", m)
        elif _has(sub, home):
            add("home", m)
        elif _has(sub, away):
            add("away", m)
    if not code:
        return prices  # game not listed yet

    # Totals ladder
    for m in k.markets(event_ticker=f'{config.KALSHI_SERIES["total"]}-{code}'):
        fs = m.get("floor_strike")
        if fs in (0.5, 1.5, 2.5, 3.5):
            add(f"over_{str(fs).replace('.', '_')}", m)
    # Both teams to score
    for m in k.markets(event_ticker=f'{config.KALSHI_SERIES["btts"]}-{code}'):
        add("btts_yes", m)
    # Spread ladder: home/away wins by 1.5+ or 2.5+
    _sp_floors = {1.5: "1.5", 2.5: "2.5"}
    for m in k.markets(event_ticker=f'{config.KALSHI_SERIES["spread"]}-{code}'):
        fs = m.get("floor_strike")
        if fs not in _sp_floors:
            continue
        sub = m.get("yes_sub_title") or ""
        suffix = _sp_floors[fs].replace(".", "_")
        if _has(sub, home):
            add(f"home_-{suffix}", m)
        elif _has(sub, away):
            add(f"away_-{suffix}", m)
    # Team totals (full ladder: 0.5 / 1.5 / 2.5 / 3.5 each team)
    tt_series = config.KALSHI_SERIES.get("team_total", "KXWCTEAMTOTAL")
    _tt_floors = {0.5: "0_5", 1.5: "1_5", 2.5: "2_5", 3.5: "3_5"}
    for m in k.markets(event_ticker=f"{tt_series}-{code}"):
        fs = m.get("floor_strike")
        if fs not in _tt_floors:
            continue
        sub = m.get("yes_sub_title") or ""
        suffix = _tt_floors[fs]
        if _has(sub, home):
            add(f"home_over_{suffix}", m)
        elif _has(sub, away):
            add(f"away_over_{suffix}", m)

    # Advances market (knockout only): find the nearest open round market for each team.
    # Rather than inferring the round from the game date (brittle — date boundaries
    # don't align perfectly with round boundaries), we fetch all open KXWCROUND markets
    # and pick the lowest/nearest round tier for each team. Round order:
    # 26RO16 < 26QUAR < 26SEMI < 26FINAL
    _ROUND_ORDER = ["26RO16", "26QUAR", "26SEMI", "26FINAL"]

    if game_date > "2026-06-27":
        round_markets = k.markets(series_ticker=config.KALSHI_SERIES["reach_round"],
                                  status="open")
        # Build {team_side: best_market} by picking nearest round
        best: dict[str, tuple[int, dict]] = {}  # side -> (round_index, market)
        for m in round_markets:
            ticker = m.get("ticker") or ""
            title = m.get("title") or ""
            tier = next((r for r in _ROUND_ORDER if r in ticker), None)
            if tier is None:
                continue
            tier_idx = _ROUND_ORDER.index(tier)
            for side, team in (("home", home), ("away", away)):
                if team_in_text(title, team):
                    if side not in best or tier_idx < best[side][0]:
                        best[side] = (tier_idx, m)
        for side, (_, m) in best.items():
            add(f"{side}_advances", m)

    return prices
