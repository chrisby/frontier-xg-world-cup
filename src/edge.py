"""Edge engine: two-sided, sharp-anchored, quarter-Kelly staking.

This is the ONLY place market prices meet the model. Hardening rules:

- Two-sided: we evaluate both YES (model says underpriced) and NO (overpriced).
- Sharp anchor: when a deep market (Polymarket) prices the same outcome, we
  only bet if it agrees with our model's direction (gate), and we SIZE on the
  more conservative of the two probabilities. If our model and a $1M+ book
  disagree, the model is probably wrong — no bet.
- Unanchored markets (no sharp line) demand a higher edge (MIN_EDGE_UNANCHORED).
- Quarter-Kelly with per-bet/per-group/total caps until CLV proves the model.

Kalshi prices are in cents (1-99). A contract bought at p cents pays $1.
"""
from dataclasses import dataclass
from math import ceil

from . import config

_EPS = 1e-9  # float-tolerance for threshold comparisons (0.7-0.64 != 0.06 exactly)


def kalshi_fee(price_dollars: float, contracts: int) -> float:
    """Kalshi trading fee ≈ ceil(0.07 * C * P * (1-P)) cents → dollars."""
    cents = ceil(0.07 * contracts * price_dollars * (1 - price_dollars) * 100)
    return cents / 100.0


def kelly_fraction(win_p: float, cost: float) -> float:
    """Fractional-Kelly bankroll fraction for a binary contract costing `cost`."""
    if cost <= 0 or cost >= 1:
        return 0.0
    full = (win_p - cost) / (1 - cost)
    return max(0.0, full) * config.KELLY_FRACTION


@dataclass
class BetRec:
    market: str          # human label, e.g. "USA to beat Turkiye"
    ticker: str
    model_p: float       # model P(THIS BET wins) — for NO bets that's 1-p(event)
    price_cents: int     # ask we'd pay for this side
    edge: float          # sizing_p - price (conservative when anchored)
    stake: float         # USD
    contracts: int
    rationale: str = ""
    group: str = ""      # correlation group (match event / team) for exposure caps
    side: str = "yes"    # which side of the contract we buy
    sharp_p: float | None = None  # sharp-line P(bet wins), when anchored

    @property
    def implied(self) -> float:
        return self.price_cents / 100.0


def _build(label, ticker, win_p, cost_cents, bankroll, rationale, group,
           side, sharp_win, size_p) -> tuple["BetRec | None", "str | None"]:
    cost = cost_cents / 100.0
    frac = kelly_fraction(size_p, cost)
    stake = min(frac * bankroll, config.MAX_STAKE_FRACTION * bankroll)
    contracts = int(stake / cost)
    if contracts < 1:
        return None, (f"{side.upper()} edge ({size_p - cost:+.1%}) is too thin to "
                       f"size even 1 contract at ${bankroll:,.0f} bankroll")
    stake = round(contracts * cost, 2)
    # the conservative edge must survive the trading fee
    if (size_p - cost) * contracts <= kalshi_fee(cost, contracts):
        return None, (f"{side.upper()} edge ({size_p - cost:+.1%}) is eaten by "
                       f"Kalshi's trading fee")
    name = label if side == "yes" else f"NO: {label}"
    rec = BetRec(name, ticker, round(win_p, 4), cost_cents,
                 round(size_p - cost, 4), stake, contracts, rationale,
                 group or ticker, side, round(sharp_win, 4) if sharp_win else None)
    return rec, None


def evaluate_market_diag(label: str, ticker: str, model_p: float, book: dict,
                         bankroll: float, sharp_p: float | None = None,
                         rationale: str = "", group: str = ""
                         ) -> tuple["BetRec | None", "str | None"]:
    """Evaluate both sides of one market.

    Returns `(rec, reason)`: `rec` is the best qualifying bet (or None), and
    `reason` is a human-readable explanation of why no bet qualified — picking
    whichever side came closest to clearing the bar — or None when a bet was
    made. `book`: {'yes_ask','no_ask',...} in cents. `sharp_p`: deep-market
    P(event) for the SAME outcome, if one exists.
    """
    candidates = []
    diagnostics = []  # (closeness, reason) — higher closeness = nearer to qualifying
    for side in ("yes", "no"):
        cost_cents = book.get(f"{side}_ask")
        if not cost_cents or not (0 < cost_cents < 100):
            diagnostics.append((-999, f"no {side.upper()} price quoted"))
            continue
        cost = cost_cents / 100.0
        win_p = model_p if side == "yes" else 1 - model_p
        if sharp_p is not None:
            sharp_win = sharp_p if side == "yes" else 1 - sharp_p
            # gate: the sharp line must also think this side is underpriced
            if sharp_win - cost < config.SHARP_GATE_MARGIN - _EPS:
                diagnostics.append((win_p - cost,
                    f"sharp line ({sharp_win:.0%}) doesn't see {side.upper()} as "
                    f"underpriced vs. {cost_cents}c (needs ≥{config.SHARP_GATE_MARGIN:.0%} "
                    f"sharp edge) — model said {win_p:.0%}"))
                continue
            size_p = min(win_p, sharp_win)   # size on the conservative view
            if size_p - cost < config.MIN_EDGE - _EPS:
                diagnostics.append((size_p - cost,
                    f"{side.upper()} edge {size_p - cost:+.1%} (model {win_p:.0%}, "
                    f"sharp {sharp_win:.0%} vs {cost_cents}c) below the "
                    f"{config.MIN_EDGE:.0%} anchored bar"))
                continue
        else:
            sharp_win, size_p = None, win_p
            if size_p - cost < config.MIN_EDGE_UNANCHORED - _EPS:
                diagnostics.append((size_p - cost,
                    f"{side.upper()} edge {size_p - cost:+.1%} (model {win_p:.0%} vs "
                    f"{cost_cents}c, no Polymarket line) below the "
                    f"{config.MIN_EDGE_UNANCHORED:.0%} unanchored bar"))
                continue
        rec, reason = _build(label, ticker, win_p, cost_cents, bankroll, rationale,
                             group, side, sharp_win, size_p)
        if rec:
            candidates.append(rec)
        else:
            diagnostics.append((size_p - cost, reason))
    if candidates:
        return max(candidates, key=lambda r: r.edge), None
    if not diagnostics:
        return None, "no market price available"
    return None, max(diagnostics, key=lambda d: d[0])[1]


def evaluate_market(label: str, ticker: str, model_p: float, book: dict,
                    bankroll: float, sharp_p: float | None = None,
                    rationale: str = "", group: str = "") -> BetRec | None:
    """Evaluate both sides of one market; return the best qualifying bet.

    `book`: {'yes_ask','no_ask',...} in cents (from KalshiReadClient.book_prices)
    `sharp_p`: deep-market P(event) for the SAME outcome, if one exists.
    """
    rec, _ = evaluate_market_diag(label, ticker, model_p, book, bankroll,
                                  sharp_p=sharp_p, rationale=rationale, group=group)
    return rec


def evaluate_outcome(label: str, ticker: str, model_p: float, yes_ask_cents: int,
                     bankroll: float, rationale: str = "", group: str = "",
                     sharp_p: float | None = None) -> BetRec | None:
    """Back-compat wrapper: YES-side only book (no bid available)."""
    return evaluate_market(label, ticker, model_p,
                           {"yes_ask": yes_ask_cents}, bankroll,
                           sharp_p=sharp_p, rationale=rationale, group=group)


def render_bet_sheet(recs: list[BetRec], bankroll: float) -> str:
    if not recs:
        return ("No bets clear the bar (edge ≥ "
                f"{config.MIN_EDGE:.0%} anchored / {config.MIN_EDGE_UNANCHORED:.0%} "
                "unanchored, sharp line must agree).")
    recs = sorted(recs, key=lambda r: -r.edge)
    lines = [
        f"BET SHEET — bankroll ${bankroll:.2f}, ¼-Kelly, "
        f"edge ≥{config.MIN_EDGE:.0%} anchored/{config.MIN_EDGE_UNANCHORED:.0%} solo, "
        f"cap {config.MAX_STAKE_FRACTION:.0%}/bet",
        "-" * 78,
    ]
    total = 0.0
    for r in recs:
        total += r.stake
        anchor = f" sharp {r.sharp_p:.0%}" if r.sharp_p is not None else " no-anchor"
        lines.append(
            f"{r.market:<36} | model {r.model_p:5.1%}{anchor} vs {r.price_cents}c "
            f"| edge {r.edge:+5.1%} | BUY {r.side.upper()} {r.contracts} = ${r.stake:6.2f}"
        )
    lines.append("-" * 78)
    lines.append(f"Total staked: ${total:.2f} ({total / bankroll:.0%} of bankroll)")
    lines.append("Each bet requires your confirmation before it is placed.")
    return "\n".join(lines)
