"""Publish the structured analysis to the public Hugo site (static/wc2026).

One structured result per analyzed match is written as JSON; the public page
reads it. Only the latest analysis per game is kept (each run overwrites).
Writes into the existing Hugo site's static/ dir so it deploys verbatim and
never touches Hugo content/layouts.
"""
import json
import time
from pathlib import Path

from . import config
from .teams import canon_team

SITE = Path("~/Documents/Website/static/wc2026").expanduser()
DATA = SITE / "data"
DOSS = DATA / "dossiers"

DESCRIPTION = (
    "An independent, reasoning-driven model for the 2026 World Cup. For each "
    "match it assembles a factual dossier (squad, form with shot quality, "
    "confirmed lineups, coach, injuries, head-to-head), then a frontier model "
    "(Anthropic's Claude Opus 4.8) reasons — bottom-up, "
    "without seeing any betting odds — to "
    "calibrated probabilities and expected goals. Only at the end is the estimate "
    "compared against the market: we bet on Kalshi solely where our number and "
    "the deep sharp line (Polymarket) agree it is mispriced, sized with "
    "quarter-Kelly. Every dossier, probability, and bet below is published in "
    "full — including stakes and running P&L. The full source code will be "
    "open-sourced on GitHub once the tournament is over."
)


def slug(home: str, away: str) -> str:
    a, b = sorted([canon_team(home), canon_team(away)])
    return f"{a}__{b}".replace(" ", "_").replace("'", "")


def bet_to_dict(rc) -> dict:
    return {
        "market": rc.market, "ticker": rc.ticker, "side": rc.side,
        "price_cents": rc.price_cents, "model_p": rc.model_p,
        "sharp_p": rc.sharp_p, "edge": rc.edge, "stake": rc.stake,
        "contracts": rc.contracts,
    }


def publish_match(result: dict):
    """Write one match's structured dossier+reasoning+bets (latest only)."""
    DOSS.mkdir(parents=True, exist_ok=True)
    key = slug(result["home"], result["away"])
    (DOSS / f"{key}.json").write_text(json.dumps(result, indent=1, default=str))


def publish_futures(n_sims: int = 20000):
    """Publish tournament-winner + Golden Boot predictions with model inputs.

    Pairs each team's Monte-Carlo championship probability with its cached
    rating (off/def ratings, tier, rationale, and — where backfilled —
    the dossier the model reasoned over), and each Golden Boot candidate's
    probability with their reasoned share of their team's goals.
    """
    from . import ledger, polymarket
    from .team_rating import _load_cache as _load_ratings
    from .tournament import get_groups, monte_carlo
    from .sportmonks import SportMonks
    from .kalshi_prices import get_winner_books, get_golden_boot_books
    from .teams import same_team, team_in_text

    sm = SportMonks()
    groups = get_groups(sm)
    names = [t for g in groups.values() for t in g]

    ratings = _load_ratings()
    R = {t: {"off_rating": v["off_rating"], "def_rating": v["def_rating"]}
         for t, v in ratings.items() if t in names}
    if len(R) < len(names):
        return f"Ratings ready for {len(R)}/{len(names)} teams — run `futures` first."

    shares_f = config.DATA_DIR / "reasoned_shares.json"
    shares = json.loads(shares_f.read_text()) if shares_f.exists() else {}
    player_team = {p: t for t, sh in shares.items() for p in sh}

    results = sm.wc_results()
    res = monte_carlo(groups, R, n=n_sims, shares=shares or None, results=results)

    def _match_book(name: str, priced: dict):
        for sub, val in priced.items():
            if same_team(name, sub) or team_in_text(sub, name):
                return val
        return None

    try:
        wbooks, gbooks = get_winner_books(), get_golden_boot_books()
    except Exception:
        wbooks, gbooks = {}, {}
    try:
        sharp_w, sharp_g = polymarket.winner_probs(), polymarket.golden_boot_probs()
    except Exception:
        sharp_w, sharp_g = {}, {}
    open_bets = ledger.futures_bets()

    def _no_bet_reason(model_p, book, sharp_p) -> str:
        if not book:
            return "No Kalshi market for this outcome yet."
        kalshi_p = book["yes_ask"] / 100.0
        if sharp_p is not None:
            model_dir, sharp_dir = model_p - kalshi_p, sharp_p - kalshi_p
            if (model_dir > 0) != (sharp_dir > 0):
                return ("Model and the sharp line (Polymarket) disagree on "
                        "direction vs. the Kalshi price — no bet.")
        return "No qualifying edge after fees and position sizing."

    def _market(name: str, model_p: float, book_map: dict, sharp_map: dict) -> dict:
        book = _match_book(name, book_map)
        sharp_p = polymarket.lookup(name, sharp_map)
        bet = open_bets.get(book["ticker"]) if book else None
        return {
            "kalshi_price": book["yes_ask"] if book else None,
            "sharp_p": sharp_p,
            "bet": bet,
            "no_bet_reason": None if bet else _no_bet_reason(model_p, book, sharp_p),
        }

    winner = []
    for team, probs in sorted(res["teams"].items(), key=lambda kv: -kv[1]["champion"]):
        rt = ratings.get(team, {})
        winner.append({
            "team": team,
            "p_champion": probs["champion"], "p_final": probs["reach_final"],
            "p_sf": probs["reach_sf"], "p_win_group": probs["win_group"],
            "off_rating": rt.get("off_rating"), "def_rating": rt.get("def_rating"),
            "tier": rt.get("tier"), "rationale": rt.get("rationale"),
            "dossier": rt.get("dossier"),
            "market": _market(team, probs["champion"], wbooks, sharp_w),
        })

    golden_boot = [
        {"player": player, "team": player_team.get(player), "p_golden_boot": p,
         "share": (shares.get(player_team.get(player)) or {}).get(player),
         "market": _market(player, p, gbooks, sharp_g)}
        for player, p in res["golden_boot"].items()
    ]

    out = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_sims": n_sims,
        "conditioned_on": len(results["group"]) + len(results["ko"]),
        "winner": winner,
        "golden_boot": golden_boot,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "futures.json").write_text(json.dumps(out, indent=1, default=str))
    return (f"Published futures.json: {len(winner)} teams, "
            f"{len(golden_boot)} Golden Boot candidates.")


_FINISHED_STATES = {"FT", "AET", "FT_PEN"}


def _final_score(fixture: dict) -> dict | None:
    """{"home": goals, "away": goals} for a finished fixture, else None."""
    if (fixture.get("state") or {}).get("developer_name") not in _FINISHED_STATES:
        return None
    loc_id = {(p.get("meta") or {}).get("location"): p.get("id")
              for p in fixture.get("participants", [])}
    goals = {s.get("participant_id"): (s.get("score") or {}).get("goals")
             for s in fixture.get("scores", []) if s.get("description") == "CURRENT"}
    home_id, away_id = loc_id.get("home"), loc_id.get("away")
    if home_id not in goals or away_id not in goals:
        return None
    return {"home": goals[home_id], "away": goals[away_id]}


def publish_site():
    """Regenerate the index: system blurb, bankroll/P&L, and the games table."""
    from . import ledger
    from .kalshi import live_bankroll
    from .sportmonks import SportMonks

    sm = SportMonks()
    scores_by_name = {f.get("name"): _final_score(f) for f in sm.wc_fixtures()}
    games = []
    from .schedule import get_schedule
    for f in get_schedule(sm):
        tt = (f.get("name") or "").split(" vs ")
        if len(tt) != 2:
            continue
        key = slug(tt[0], tt[1])
        games.append({
            "name": f["name"], "home": tt[0].strip(), "away": tt[1].strip(),
            "kickoff": f["start"], "stage": f["stage"], "key": key,
            "placeholder": f.get("placeholder", False),
            "analysis": ledger.analysis_for(tt[0], tt[1]),
            "has_dossier": (DOSS / f"{key}.json").exists(),
            "score": scores_by_name.get(f.get("name")),
        })
    import zoneinfo
    from datetime import datetime, timezone as _tz
    _PT = zoneinfo.ZoneInfo("America/Los_Angeles")

    def _pt_date(ts):
        if not ts:
            return ""
        dt = datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=_tz.utc)
        return dt.astimezone(_PT).strftime("%Y-%m-%d")

    game_to_date = {}
    for g in games:
        key = ledger.game_key(g["home"], g["away"])
        game_to_date[key] = _pt_date(g["kickoff"])

    _START = 255.0
    settled = ledger._conn().execute(
        "SELECT settled_ts, pnl, game, market, status, fee FROM bets "
        "WHERE status IN ('won','lost') AND settled_ts IS NOT NULL "
        "ORDER BY settled_ts"
    ).fetchall()
    game_order, game_bets = [], {}
    for ts, pnl, game, market, status, fee in settled:
        if game not in game_bets:
            game_order.append(game)
            game_bets[game] = []
        game_bets[game].append({"ts": ts, "pnl": round(pnl, 2),
                                 "market": market, "status": status,
                                 "fee": round(fee or 0, 2)})
    # Sort by game_date so the chart line follows actual match order, not ledger entry order
    game_order.sort(key=lambda g: game_to_date.get(g, "9999"))
    running = _START
    bk_history = [{"ts": None, "bankroll": _START}]
    for game in game_order:
        bets = game_bets[game]
        total_pnl = round(sum(b["pnl"] for b in bets), 2)
        running = round(running + total_pnl, 2)
        bk_history.append({"ts": max(b["ts"] for b in bets), "bankroll": running,
                            "pnl": total_pnl, "game": game, "bets": bets,
                            "game_date": game_to_date.get(game, "")})

    # Snap the last history entry to the live Kalshi balance so the tooltip
    # shows the accurate current value rather than the ledger-computed total
    # (small rounding/fee discrepancies accumulate over many bets).
    _live = live_bankroll()
    if _live is not None and len(bk_history) > 1:
        bk_history[-1]["bankroll"] = round(_live, 2)

    open_stake = round(sum(
        r[0] for r in ledger._conn().execute(
            "SELECT stake FROM bets WHERE status='open' AND placed=1"
        ).fetchall()
    ), 2)

    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "site.json").write_text(json.dumps({
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "description": DESCRIPTION,
        "bankroll": live_bankroll() or config.BANKROLL_USD,
        "open_stake": open_stake,
        "ledger": ledger.summary(),
        "games": games,
        "bankroll_history": bk_history,
    }, indent=1, default=str))
