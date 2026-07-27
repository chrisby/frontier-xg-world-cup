"""Map one match's model outputs to bet candidates across ALL its Kalshi markets.

This is the money layer: the match reasoner gives 1X2 + expected goals, goals.py
derives totals/BTTS/spreads/team-totals, and this turns every one of them into a
two-sided, sharp-anchored edge check. All markets of a single game share one
correlation group, so the portfolio cap limits total exposure on that game.

`prices` maps a market key to {"ticker":…, "yes_ask":…, "no_ask":…} (cents).
1X2 outcomes are anchored against Polymarket's match book when it exists;
derived markets (no sharp line) face the higher unanchored edge threshold.
"""
from . import config, edge


def build_candidates(home: str, away: str, r: dict, dm: dict,
                     prices: dict, scorers: list | None = None,
                     bankroll: float | None = None) -> tuple[list, list]:
    """Return `(bets, no_bets)` across the match's market board.

    `bets` is the edge-qualified BetRecs. `no_bets` is a list of
    `{"market": label, "reason": str}` for every other market on the board,
    explaining why it didn't clear the bar (no Kalshi market yet, sharp-line
    gate, edge below the anchored/unanchored threshold, or fee/sizing).
    model_p for each market comes from the reasoner (1X2) or goals.py (derived).
    """
    group = f"{home}-{away}"
    bankroll = bankroll if bankroll is not None else config.BANKROLL_USD
    rationale = (r.get("rationale", "") or "")[:240]

    from .polymarket import match_probs
    sharp = match_probs(home, away)  # {} if Polymarket doesn't list this game

    # In knockout, P(team advances) = P(win in regulation) + P(draw) * 0.5
    # (draws go to ET/pens, modelled as coin flip between the two sides)
    p_home_adv = round(r["p_home"] + r["p_draw"] * 0.5, 4)
    p_away_adv = round(r["p_away"] + r["p_draw"] * 0.5, 4)

    # market key -> (human label, model probability)
    board = {
        "home":           (f"{home} to beat {away}", r["p_home"]),
        "draw":           (f"{home}-{away} draw", r["p_draw"]),
        "away":           (f"{away} to beat {home}", r["p_away"]),
        "home_advances":  (f"{home} advances (incl. ET/pens)", p_home_adv),
        "away_advances":  (f"{away} advances (incl. ET/pens)", p_away_adv),
        "over_0_5":     (f"{home}-{away} over 0.5 goals", dm["over_0_5"]),
        "over_1_5":     (f"{home}-{away} over 1.5 goals", dm["over_1_5"]),
        "over_2_5":     (f"{home}-{away} over 2.5 goals", dm["over_2_5"]),
        "over_3_5":     (f"{home}-{away} over 3.5 goals", dm["over_3_5"]),
        "btts_yes":     (f"{home}-{away} both teams score", dm["btts_yes"]),
        "home_-1.5":    (f"{home} -1.5 (win by 2+)", dm["home_cover_-1.5"]),
        "home_-2.5":    (f"{home} -2.5 (win by 3+)", dm["home_cover_-2.5"]),
        "away_-1.5":    (f"{away} -1.5 (win by 2+)", dm["away_cover_-1.5"]),
        "away_-2.5":    (f"{away} -2.5 (win by 3+)", dm["away_cover_-2.5"]),
        "home_over_0_5":(f"{home} over 0.5 team goals", dm["home_over_0_5"]),
        "home_over_1_5":(f"{home} over 1.5 team goals", dm["home_over_1_5"]),
        "home_over_2_5":(f"{home} over 2.5 team goals", dm["home_over_2_5"]),
        "home_over_3_5":(f"{home} over 3.5 team goals", dm["home_over_3_5"]),
        "away_over_0_5":(f"{away} over 0.5 team goals", dm["away_over_0_5"]),
        "away_over_1_5":(f"{away} over 1.5 team goals", dm["away_over_1_5"]),
        "away_over_2_5":(f"{away} over 2.5 team goals", dm["away_over_2_5"]),
        "away_over_3_5":(f"{away} over 3.5 team goals", dm["away_over_3_5"]),
    }
    # anytime-goalscorer markets, keyed scorer:<name>
    for s in (scorers or []):
        board[f"scorer:{s['name']}"] = (f"{s['name']} to score", s["p_anytime"])

    out, no_bets = [], []
    for key, (label, p) in board.items():
        book = prices.get(key)
        if not book:
            no_bets.append({"market": label, "reason": "no Kalshi market for this outcome yet"})
            continue
        rec, reason = edge.evaluate_market_diag(label, book["ticker"], p, book, bankroll,
                                                 sharp_p=sharp.get(key),  # 1X2 keys only
                                                 rationale=rationale, group=group)
        if rec:
            out.append(rec)
        else:
            no_bets.append({"market": label, "reason": reason})

    # home_advances and away_advances are mutually exclusive in a knockout game
    # (one team advances, the other doesn't). Keeping both doubles exposure on a
    # single outcome. Drop the lower-edge one if both cleared the threshold.
    adv_recs = [r for r in out if "advances" in r.market]
    if len(adv_recs) == 2:
        drop = min(adv_recs, key=lambda r: r.edge)
        out.remove(drop)
        no_bets.append({"market": drop.market,
                        "reason": f"correlated with the other advances bet — kept higher-edge side only"})

    return out, no_bets
