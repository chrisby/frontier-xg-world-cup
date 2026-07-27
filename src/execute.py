"""Confirm-and-place: the human-approved final step.

Hardened: refuses to place when the kill-switch has tripped (model demonstrably
behind the close), places the correct side (YES or NO), and records a bet as
PLACED in the ledger only after the order actually succeeds.
"""
from . import edge, ledger
from .edge import BetRec


def confirm_and_place(recs: list[BetRec], bankroll: float, mode: str,
                      auto_yes: bool = False,
                      override_kill: bool = False,
                      game: str | None = None) -> list[dict]:
    """Place the approved bets. Returns per-bet results (also usable over HTTP)."""
    if not recs:
        print("No qualifying bets to place.")
        return []

    tripped, reason = ledger.kill_switch()
    if tripped and not override_kill:
        msg = (f"KILL-SWITCH {reason}. Placement blocked — review the model "
               f"before overriding (override_kill=True / --override-kill).")
        print(msg)
        return [{"ok": False, "market": r.market, "msg": msg} for r in recs]

    if not auto_yes:  # CLI path: require typed confirmation
        print(edge.render_bet_sheet(recs, bankroll))
        total = sum(r.stake for r in recs)
        ans = input(f"\nPlace these {len(recs)} bets (${total:.2f}) on Kalshi? "
                    f"type 'yes' to confirm: ").strip().lower()
        if ans != "yes":
            print("Aborted — nothing placed.")
            return []

    from .kalshi import KalshiTradeClient
    client = KalshiTradeClient()
    results = []
    for r in recs:
        try:
            resp = client.place_order(r.ticker, r.side, "buy",
                                      r.contracts, r.price_cents)
            oid = (resp.get("order") or {}).get("order_id", "?")
            ledger.record(r, mode=mode, rationale=r.rationale, placed=True, game=game)
            msg = (f"placed {r.market} (BUY {r.side.upper()} {r.contracts} "
                   f"@ {r.price_cents}c) order {oid}")
            results.append({"ok": True, "market": r.market, "msg": msg})
            print("  ✓", msg)
        except Exception as e:  # noqa: BLE001 - surface any placement failure
            ledger.record(r, mode=mode, rationale=r.rationale, placed=False, game=game)
            results.append({"ok": False, "market": r.market, "msg": str(e)})
            print(f"  ✗ FAILED {r.market}: {e}")
    return results
