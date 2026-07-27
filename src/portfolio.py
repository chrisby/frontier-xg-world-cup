"""Portfolio-level staking: cap correlated and total exposure.

Half-Kelly sizes each bet as if it were the only one. But bets that share a team
or a match move together, so stacking them over-concentrates risk. This greedy
allocator takes the edge-qualified candidate bets, prioritizes the highest-edge
ones, and trims/drops to respect:
  - per-group cap   (one team or one match)  -> MAX_GROUP_FRACTION
  - total cap       (all deployed at once)    -> MAX_PORTFOLIO_FRACTION
"""
from dataclasses import replace

from . import config
from .edge import BetRec, kalshi_fee


def allocate(candidates: list[BetRec], bankroll: float,
             open_exposure: dict | None = None) -> list[BetRec]:
    """Return the accepted bets with stakes trimmed to fit exposure caps.

    `open_exposure` optionally carries already-placed stakes per group so caps
    account for live positions.
    """
    group_cap = config.MAX_GROUP_FRACTION * bankroll
    total_cap = config.MAX_PORTFOLIO_FRACTION * bankroll
    group_used = dict(open_exposure or {})
    total_used = sum(group_used.values())

    out = []
    for rec in sorted(candidates, key=lambda r: -r.edge):
        g = rec.group
        room = min(group_cap - group_used.get(g, 0.0), total_cap - total_used)
        if room <= 0:
            continue
        stake = min(rec.stake, room)
        cost = rec.price_cents / 100.0
        contracts = int(stake / cost)
        if contracts < 1:
            continue
        stake = round(contracts * cost, 2)
        # keep only if the (conservative, anchored) edge still beats the fee
        if rec.edge * contracts <= kalshi_fee(cost, contracts):
            continue
        out.append(replace(rec, stake=stake, contracts=contracts))
        group_used[g] = group_used.get(g, 0.0) + stake
        total_used += stake
    return out
