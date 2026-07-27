"""Offline match pipeline: Kalshi match -> dossier -> reasoning -> bet sheet.

Usage:
    python -m src.orchestrator markets          # list tradeable WC match markets
    python -m src.orchestrator match "Japan" "Sweden"
"""
import os
import re
import sys

from . import config, edge
from .dossier import build_match_dossier, render_markdown
from .kalshi import KalshiReadClient
from .sportmonks import SportMonks
from .teams import build_name_index, resolve


def bankroll() -> float:
    """Live Kalshi balance if available, else the configured fallback."""
    from .kalshi import live_bankroll
    return live_bankroll() or config.BANKROLL_USD


def list_match_markets(k: KalshiReadClient) -> list[dict]:
    """Group open KXWCGAME markets into matches with their 3 outcome tickers."""
    markets = k.markets(series_ticker=config.KALSHI_SERIES["match"], status="open")
    by_event: dict[str, dict] = {}
    for m in markets:
        ev = m.get("event_ticker") or m.get("ticker", "").rsplit("-", 1)[0]
        slot = by_event.setdefault(ev, {"title": m.get("title"), "outcomes": []})
        slot["outcomes"].append({
            "ticker": m.get("ticker"),
            "label": m.get("yes_sub_title") or m.get("ticker", "").rsplit("-", 1)[-1],
            "yes_ask": m.get("yes_ask"),
            "close": m.get("close_time"),
        })
    return list(by_event.values())


def _teams_from_title(title: str) -> tuple[str, str] | None:
    m = re.match(r"(.+?)\s+vs\.?\s+(.+?)\s+Winner", title or "", re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


_LAST_RECS: dict = {}    # (home, away) -> recs, so the web can place after analyze
_LAST_RESULT: dict = {}  # (home, away) -> structured analysis (for web + publish)


def _recs_from_dossier(home: str, away: str) -> list[edge.BetRec]:
    """Rebuild BetRecs from the last-published dossier, skipping any bets
    already marked placed — used when the bets were loaded from disk rather
    than freshly analyzed in this process."""
    import json
    from . import ledger, publish as _pub
    f = _pub.DOSS / f"{_pub.slug(home, away)}.json"
    if not f.exists():
        return []
    data = json.loads(f.read_text())
    placed = ledger.placed_tickers(ledger.game_key(home, away))
    return [
        edge.BetRec(b["market"], b["ticker"], b["model_p"], b["price_cents"],
                    b["edge"], b["stake"], b["contracts"], "", b["ticker"],
                    b["side"], b.get("sharp_p"))
        for b in data.get("bets", [])
        if (b["ticker"], b["side"]) not in placed
    ]


def place_cached(home: str, away: str) -> list[dict]:
    """Place the bets from the most recent analyze() of this match (web flow),
    falling back to the last-published dossier if this process never
    analyzed the match (e.g. bets were loaded from a saved dossier)."""
    from . import ledger
    from .execute import confirm_and_place
    recs = _LAST_RECS.get((home, away))
    if recs is None:
        recs = _recs_from_dossier(home, away)
    results = confirm_and_place(recs, bankroll(), "match", auto_yes=True,
                                game=ledger.game_key(home, away))
    ledger.bump_placed(home, away, sum(1 for x in results if x.get("ok")))
    try:  # refresh the public site's placed counts + P&L
        from . import publish
        publish.publish_site()
    except Exception:
        pass
    return results


def run_match(home: str, away: str, market_prices: dict | None = None,
              place: bool = False, record: bool = True) -> str:
    """Build dossier, reason (if API key present), and produce a bet sheet.

    `market_prices`: {"home"|"draw"|"away": yes_ask_cents} from Kalshi. When the
    books aren't open yet these are None and we emit the analysis only.
    """
    sm = SportMonks()
    try:
        idx = build_name_index(sm)
    except Exception as e:
        raise RuntimeError(
            f"SportMonks is unreachable — cannot build dossier without live squad/form data. "
            f"Check your SPORTMONKS_API_KEY and connectivity. ({e})"
        ) from e
    hid, aid = resolve(sm, home, idx), resolve(sm, away, idx)
    if not hid or not aid:
        return f"Could not resolve teams to SportMonks IDs: {home}->{hid}, {away}->{aid}"

    # Detect knockout phase: Kalshi R16+ tickers contain JUL (games move to July)
    k = KalshiReadClient()
    phase = "group"
    try:
        match_markets = k.markets(series_ticker=config.KALSHI_SERIES["match"], status="open")
        from .teams import same_team
        for m in match_markets:
            title = m.get("title", "")
            occurrence = m.get("occurrence_datetime", "") or ""
            start_date = occurrence[:10].replace("T", "")
            if same_team(home, title) and same_team(away, title) and start_date > "2026-06-27":
                phase = "knockout"
                break
    except Exception:
        pass
    ctx = {"tournament": "2026 World Cup", "venue_hosts": ["USA", "Canada", "Mexico"],
           "phase": phase}
    dossier = build_match_dossier(sm, hid, aid, ctx)
    brief = render_markdown(dossier)

    if not dossier["home"]["lineup"] or not dossier["away"]["lineup"]:
        missing = ", ".join(
            t for t, d in ((home, dossier["home"]), (away, dossier["away"]))
            if not d["lineup"])
        return (brief + f"\n\n[Starting XI not yet confirmed for {missing} — "
                "skipping the reasoning pass to avoid spending tokens on a "
                "less-informed estimate. Re-run once lineups are out.]")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return (brief + "\n\n[Set ANTHROPIC_API_KEY to run the reasoning layer "
                "and generate probabilities + bet sheet.]")

    from .reasoning import reason_match
    from .goals import derive_markets
    r = reason_match(brief, dossier["home"]["name"], dossier["away"]["name"], phase=phase)
    # If the model considers a team the clear favourite (>55% win prob), nudge
    # their xg up — corrects a systematic underestimation bias for favourites
    # that prompting alone doesn't fix.
    xg_home = r["xg_home"] * 1.35 if (r["p_home"] > 0.55 and phase == "group") else r["xg_home"]
    xg_away = r["xg_away"] * 1.35 if (r["p_away"] > 0.55 and phase == "group") else r["xg_away"]
    dm = derive_markets(xg_home, xg_away)

    out = [brief, "", "## Model reasoning", r["rationale"],
           "Key factors: " + "; ".join(r["key_factors"]),
           f"Probabilities — {home} {r['p_home']:.1%} / draw {r['p_draw']:.1%} / "
           f"{away} {r['p_away']:.1%}  (confidence: {r['confidence']})",
           f"Expected goals — {home} {xg_home:.2f}, {away} {xg_away:.2f}",
           "", "## Derived markets (from expected goals)",
           f"  Over 2.5 goals: {dm['over_2_5']:.0%} | Over 1.5: {dm['over_1_5']:.0%} "
           f"| Over 3.5: {dm['over_3_5']:.0%}",
           f"  Both teams to score: {dm['btts_yes']:.0%} | "
           f"{home} clean sheet: {dm['home_clean_sheet']:.0%} | "
           f"{away} clean sheet: {dm['away_clean_sheet']:.0%}",
           f"  {home} win by 2+: {dm['home_cover_-1.5']:.0%} | "
           f"{home} over 1.5 goals: {dm['home_over_1_5']:.0%} | "
           f"{away} over 1.5 goals: {dm['away_over_1_5']:.0%}",
           f"  [Poisson cross-check of 1X2 from xG: "
           f"{dm['p_home']:.0%}/{dm['p_draw']:.0%}/{dm['p_away']:.0%}]", ""]

    if market_prices is None:  # auto-fetch live Kalshi prices for this game
        from .kalshi_prices import get_match_prices
        market_prices = get_match_prices(home, away)
    if not market_prices:
        out.append("[No live Kalshi prices yet — bet sheet will populate once the "
                   "match order book opens.]")
        return "\n".join(out)

    from . import portfolio, ledger, match_markets
    bank = bankroll()
    # market_prices: {market_key: (kalshi_ticker, yes_ask_cents)} for priced markets
    cands, no_bets = match_markets.build_candidates(home, away, r, dm, market_prices, bankroll=bank)
    recs = portfolio.allocate(cands, bank)
    _LAST_RECS[(home, away)] = recs  # cache for a later place_cached()
    # candidates that cleared edge.py but got dropped by portfolio exposure caps
    accepted = {(rc.ticker, rc.side) for rc in recs}
    for rec in cands:
        if (rec.ticker, rec.side) not in accepted:
            no_bets.append({"market": rec.market,
                            "reason": (f"edge qualified ({rec.edge:+.1%}) but the "
                                       "portfolio exposure cap for this match/team "
                                       "is already full")})
    # always log that this match was analyzed (even with 0 bets = "no edge")
    ledger.record_analysis(
        home, away,
        f"{home} {r['p_home']:.0%}/draw {r['p_draw']:.0%}/{away} {r['p_away']:.0%}",
        len(recs), model=r["model"], prompt_snapshot=r.get("prompt_snapshot"))
    # assemble the single structured result that feeds web + public site + bets
    import time as _t
    from . import publish as _pub
    structured = {
        "home": home, "away": away,
        "reasoning": {**{k: r[k] for k in ("p_home", "p_draw", "p_away",
                                           "confidence", "key_factors",
                                           "rationale", "model", "prompt_snapshot")},
                      "xg_home": xg_home, "xg_away": xg_away},
        "derived": dm,
        "home_dossier": dossier["home"], "away_dossier": dossier["away"],
        "head_to_head": dossier.get("head_to_head", []),
        "bets": [_pub.bet_to_dict(rc) for rc in recs],
        "no_bets": no_bets,
        "bankroll": bank,
        "updated": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
    }
    _LAST_RESULT[(home, away)] = structured
    try:  # publish to the public site (best-effort — never block analysis)
        _pub.publish_match(structured)
        _pub.publish_site()
    except Exception:
        pass
    if place:
        from .execute import confirm_and_place
        out.append(edge.render_bet_sheet(recs, bank))
        print("\n".join(out))
        confirm_and_place(recs, bank, "match", game=ledger.game_key(home, away))
        return ""
    if record:
        for rc in recs:  # log every recommended bet WITH its reasoning
            ledger.record(rc, mode="match", confidence=r["confidence"],
                          rationale=r["rationale"], key_factors=r["key_factors"],
                          game=ledger.game_key(home, away))
    out.append(edge.render_bet_sheet(recs, bank))
    return "\n".join(out)


def run_futures(n_sims: int = 20000, limit: int | None = None) -> str:
    """Build/load reasoned ratings, Monte-Carlo the bracket, rank futures probs."""
    from .team_rating import rate_team, refresh_played_teams
    from .tournament import get_groups, monte_carlo
    from .teams import build_name_index, resolve

    sm = SportMonks()
    groups = get_groups(sm)
    idx = build_name_index(sm)
    names = [t for g in groups.values() for t in g]
    if limit:
        names = names[:limit]

    name_to_id = {name: resolve(sm, name, idx) for name in names}
    refreshed = refresh_played_teams(sm, name_to_id)

    R, missing = {}, []
    for name in names:
        tid = name_to_id.get(name)
        if not tid:
            missing.append(name)
            continue
        R[name] = rate_team(sm, tid, name)  # cached to data/ratings.json

    rated = {t: R[t] for t in groups_flat(groups) if t in R}
    if len(rated) < sum(len(v) for v in groups.values()):
        return (f"Ratings ready for {len(rated)}/48 teams. Run `futures` again to "
                f"finish the rating build (cached), then the simulation will run.")

    shares = _load_shares()  # {team: {player: goal_share}}; {} until built
    results = sm.wc_results()  # condition on games already played (no-op pre-tournament)
    res = monte_carlo(groups, rated, n=n_sims, shares=shares or None, results=results)
    n_done = len(results["group"]) + len(results["ko"])
    ranked = sorted(res["teams"].items(), key=lambda kv: -kv[1]["champion"])
    out = [f"WORLD CUP FUTURES — {n_sims:,} simulations", "-" * 60,
           f"{'Team':<22}{'Champ':>7}{'Final':>7}{'SF':>6}{'Win Grp':>9}{'Advance':>9}"]
    for t, p in ranked[:24]:
        out.append(f"{t:<22}{p['champion']:>7.1%}{p['reach_final']:>7.1%}"
                   f"{p['reach_sf']:>6.1%}{p['win_group']:>9.1%}{p['advance']:>9.1%}")
    if res["golden_boot"]:
        out.append("\nGolden Boot (top scorer) probabilities:")
        for pl, pr in list(res["golden_boot"].items())[:12]:
            out.append(f"  {pl:<24}{pr:>6.1%}")
    out.insert(1, f"(conditioned on {n_done} completed game(s))" if n_done
               else "(pre-tournament: no games played yet)")
    if refreshed:
        out.insert(2, f"(re-rated after their WC match: {', '.join(refreshed)})")
    out.append("\nCompare to Kalshi KXMENWORLDCUP (winner) and KXWCGOALLEADER "
               "(golden boot); same ½-Kelly engine as match bets.")
    return "\n".join(out)


def _load_shares() -> dict:
    """Goal shares for the Golden Boot sim: arithmetic baseline, with Fable-
    reasoned shares overriding for the contender teams we've reasoned."""
    import json
    base = config.DATA_DIR / "shares.json"
    reasoned = config.DATA_DIR / "reasoned_shares.json"
    shares = json.loads(base.read_text()) if base.exists() else {}
    if reasoned.exists():
        shares.update(json.loads(reasoned.read_text()))  # reasoned wins per team
    return shares


def build_reasoned_shares(tiers=None) -> str:
    """Replace noisy arithmetic goal-shares with Fable-reasoned ones.

    Reasons ALL rated teams by default — the arithmetic shares can be
    catastrophically wrong on any team (e.g. a single player at 100%), so we
    don't restrict to contenders.
    """
    import json
    from .dossier import build_team_dossier
    from .props import reason_goal_shares
    from .teams import build_name_index, resolve

    ratings_f = config.DATA_DIR / "ratings.json"
    if not ratings_f.exists():
        return "Build team ratings first (futures)."
    ratings = json.loads(ratings_f.read_text())
    targets = [n for n, v in ratings.items()
               if tiers is None or v.get("tier") in tiers]

    out_f = config.DATA_DIR / "reasoned_shares.json"
    cache = json.loads(out_f.read_text()) if out_f.exists() else {}
    sm = SportMonks()
    idx = build_name_index(sm)
    done = 0
    for name in targets:
        if name in cache:
            continue
        tid = resolve(sm, name, idx)
        if not tid:
            continue
        players = build_team_dossier(sm, tid)["players"]
        sh = reason_goal_shares(name, players)
        if sh:
            cache[name] = sh
            out_f.write_text(json.dumps(cache, indent=1))
            done += 1
    return (f"Reasoned Golden-Boot goal-shares for {done} new team(s); "
            f"{len(cache)}/{len(targets)} contenders cached "
            f"(data/reasoned_shares.json). Re-run `futures-bets` to use them.")


def backfill_dossiers() -> str:
    """Attach the dossier input to already-rated teams missing it (deterministic).

    Run once after the rating build finishes — newly-rated teams store the
    dossier inline automatically.
    """
    import json
    from .dossier import build_team_dossier, render_markdown
    from .teams import build_name_index, resolve

    f = config.DATA_DIR / "ratings.json"
    if not f.exists():
        return "No ratings yet."
    sm = SportMonks()
    idx = build_name_index(sm)
    done = 0
    for name in list(json.loads(f.read_text())):
        cache = json.loads(f.read_text())
        if cache[name].get("dossier"):
            continue
        tid = resolve(sm, name, idx)
        if not tid:
            continue
        cache[name]["dossier"] = render_markdown(build_team_dossier(sm, tid))
        f.write_text(json.dumps(cache, indent=1))
        done += 1
    return f"Attached dossiers to {done} teams (now in data/ratings.json)."


def _match_price(name: str, priced: dict):
    """Match a team/player name to a {sub_title: (ticker, ask)} price map."""
    from .teams import same_team, team_in_text
    for sub, val in priced.items():
        if same_team(name, sub) or team_in_text(sub, name):
            return val
    return None


def run_futures_bets(n_sims: int = 20000, place: bool = False) -> str:
    """Winner (KXMENWORLDCUP) + Golden Boot (KXWCGOALLEADER) bets from the sim.

    Both are anchored against Polymarket's deep books (winner ~$1.9B volume,
    boot ~$4.8M): we only bet where the sim AND the sharp line agree Kalshi is
    off, sized on the conservative probability.
    """
    from . import edge, ledger, portfolio, polymarket
    from .team_rating import rate_team, refresh_played_teams
    from .tournament import get_groups, monte_carlo
    from .teams import build_name_index, resolve
    from .kalshi_prices import get_winner_books, get_golden_boot_books

    sm = SportMonks()
    groups = get_groups(sm)
    idx = build_name_index(sm)
    names = [t for g in groups.values() for t in g]
    name_to_id = {name: resolve(sm, name, idx) for name in names}
    refreshed = refresh_played_teams(sm, name_to_id)
    R = {}
    for name in names:
        tid = name_to_id.get(name)
        if tid:
            R[name] = rate_team(sm, tid, name)
    if len(R) < len(names):
        return (f"Ratings ready for {len(R)}/{len(names)} teams — run again to "
                f"finish the cached rating build, then bets will generate.")

    results = sm.wc_results()  # condition on completed games (no-op pre-tournament)
    res = monte_carlo(groups, R, n=n_sims, shares=_load_shares() or None,
                      results=results)
    wbooks, gbooks = get_winner_books(), get_golden_boot_books()
    sharp_w, sharp_g = polymarket.winner_probs(), polymarket.golden_boot_probs()
    bank = bankroll()

    # always show the computed probabilities (visible even before prices exist)
    prob = ["WORLD CUP FUTURES — model probabilities (sharp = Polymarket)",
            "-" * 56]
    if refreshed:
        prob.append(f"(re-rated after their WC match: {', '.join(refreshed)})")
    prob.append("Title odds:")
    for t, p in sorted(res["teams"].items(), key=lambda kv: -kv[1]["champion"])[:14]:
        sp = polymarket.lookup(t, sharp_w)
        prob.append(f"  {t:<22}{p['champion']:>6.1%}"
                    + (f"   sharp {sp:>5.1%}" if sp is not None else ""))
    if res["golden_boot"]:
        prob.append("\nGolden Boot (top scorer):")
        for pl, pr in list(res["golden_boot"].items())[:12]:
            sp = polymarket.lookup(pl, sharp_g)
            prob.append(f"  {pl:<24}{pr:>6.1%}"
                        + (f"   sharp {sp:>5.1%}" if sp is not None else ""))
    prob_txt = "\n".join(prob)

    cands = []
    for team, probs in res["teams"].items():
        book = _match_price(team, wbooks)
        if book:
            rc = edge.evaluate_market(f"{team} to win the World Cup",
                                      book["ticker"], probs["champion"], book,
                                      bank, sharp_p=polymarket.lookup(team, sharp_w),
                                      group=f"WIN:{team}")
            if rc:
                cands.append(rc)
    for player, prob_b in res["golden_boot"].items():
        sp = polymarket.lookup(player, sharp_g)
        if sp is None:
            continue  # player props REQUIRE a sharp anchor — our player model is
            #            artifact-prone, so no deep-market check ⇒ no bet
        book = _match_price(player, gbooks)
        if book:
            rc = edge.evaluate_market(f"{player} — Golden Boot", book["ticker"],
                                      prob_b, book, bank, sharp_p=sp,
                                      group=f"BOOT:{player}")
            if rc:
                cands.append(rc)
    recs = portfolio.allocate(cands, bank)
    if place:
        from .execute import confirm_and_place
        confirm_and_place(recs, bank, "futures")
        return prob_txt
    for rc in recs:
        ledger.record(rc, mode="futures", rationale=rc.rationale)
    sheet = edge.render_bet_sheet(recs, bank)
    return prob_txt + "\n\n" + sheet


def groups_flat(groups: dict) -> list:
    return [t for g in groups.values() for t in g]


def main(argv):
    if argv and argv[0] == "futures":
        limit = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None
        print(run_futures(limit=limit))
        return
    if argv and argv[0] == "futures-bets":
        print(run_futures_bets(place="--place" in argv))
        return
    if argv and argv[0] == "publish":
        from . import publish
        publish.publish_site()
        print(f"Published site index to {publish.DATA/'site.json'}")
        return
    if argv and argv[0] == "publish-futures":
        from . import publish
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 20000
        print(publish.publish_futures(n_sims=n))
        return
    if argv and argv[0] == "backfill-dossiers":
        print(backfill_dossiers())
        return
    if argv and argv[0] == "reason-boot-shares":
        print(build_reasoned_shares())
        return
    if argv and argv[0] == "schedule":
        sm = SportMonks()
        fixtures = [f for f in sm.schedule() if not f["placeholder"]]
        print(f"{len(fixtures)} scheduled fixtures with confirmed teams:\n")
        for f in fixtures:
            print(f"  {f['start']}  {f['stage']:<12}  {f['name']}")
        return
    if argv and argv[0] == "summary":
        from . import ledger
        print(ledger.summary())
        return
    if argv and argv[0] == "review":
        from . import ledger
        print(ledger.review_reasoning())
        return
    if argv and argv[0] == "settle" and len(argv) >= 3:
        from . import ledger, publish
        close = float(argv[3]) if len(argv) > 3 else None
        ledger.settle(int(argv[1]), won=(argv[2].lower() == "won"), close_price=close)
        publish.publish_site()
        print(f"settled bet {argv[1]} as {argv[2]}")
        return
    if not argv or argv[0] == "markets":
        k = KalshiReadClient()
        matches = list_match_markets(k)
        print(f"{len(matches)} World Cup match markets on Kalshi:\n")
        for mt in matches[:40]:
            asks = [o["yes_ask"] for o in mt["outcomes"]]
            live = "LIVE" if any(asks) else "not yet priced"
            print(f"  {mt['title']:<34} [{live}]")
        return
    if argv[0] == "match" and len(argv) >= 3:
        print(run_match(argv[1], argv[2], place="--place" in argv))
        return
    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
