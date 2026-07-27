"""Bet ledger + learning loop.

Every bet is recorded WITH the reasoning that produced it. Lifecycle:
  - `record(placed=False)` → an open RECOMMENDATION (deduped per ticker+side)
  - `record(placed=True)`  → an actually PLACED bet (real money)
  - `settle(id, won, close_price)` → outcome + closing price for CLV

P&L / ROI / CLV / Brier are computed over PLACED bets only, so hypothetical
recommendations never pollute the performance stats. The kill-switch trips when
enough settled placed bets show the model is behind the close — placement is
then blocked until you consciously override.
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import config
from .edge import BetRec
from .teams import canon_team

_DB = config.DATA_DIR / "ledger.db"


def _game_key(home: str, away: str) -> str:
    return "|".join(sorted([canon_team(home), canon_team(away)]))


def game_key(home: str, away: str) -> str:
    return _game_key(home, away)


def _conn():
    c = sqlite3.connect(_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, mode TEXT, market TEXT, ticker TEXT,
        model_p REAL, price REAL, edge REAL, stake REAL, contracts INTEGER,
        confidence TEXT, rationale TEXT, key_factors TEXT,
        status TEXT DEFAULT 'open', close_price REAL, pnl REAL, settled_ts TEXT,
        side TEXT DEFAULT 'yes', sharp REAL, placed INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS analyses (
        game TEXT PRIMARY KEY, ts TEXT, summary TEXT,
        n_rec INTEGER DEFAULT 0, n_placed INTEGER DEFAULT 0)""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(bets)")]
    for col, ddl in [("side", "TEXT DEFAULT 'yes'"), ("sharp", "REAL"),
                     ("placed", "INTEGER DEFAULT 0"), ("game", "TEXT")]:
        if col not in cols:
            c.execute(f"ALTER TABLE bets ADD COLUMN {col} {ddl}")
    acols = [r[1] for r in c.execute("PRAGMA table_info(analyses)")]
    if "model" not in acols:
        c.execute("ALTER TABLE analyses ADD COLUMN model TEXT")
        c.execute("UPDATE analyses SET model='Claude Fable 5' WHERE model IS NULL")
    if "prompt_snapshot" not in acols:
        c.execute("ALTER TABLE analyses ADD COLUMN prompt_snapshot TEXT")
    c.execute("UPDATE bets SET status='open' WHERE status='pending'")
    c.commit()
    return c


def placed_tickers(game: str) -> set[tuple[str, str]]:
    """(ticker, side) pairs already placed for this game — used to avoid
    re-placing bets that were loaded from a saved dossier."""
    c = _conn()
    rows = c.execute("SELECT ticker, side FROM bets WHERE game=? AND placed=1",
                     (game,)).fetchall()
    return {(r[0], r[1]) for r in rows}


def record(rec: BetRec, mode: str, confidence: str = "",
           rationale: str = "", key_factors: list | None = None,
           placed: bool = False, game: str | None = None) -> int:
    """Record a bet. Un-placed recommendations are deduped (refreshed in place)."""
    c = _conn()
    now = datetime.now(timezone.utc).isoformat()
    if not placed:
        row = c.execute(
            "SELECT id FROM bets WHERE ticker=? AND side=? AND status='open' "
            "AND placed=0", (rec.ticker, rec.side)).fetchone()
        if row:
            c.execute(
                """UPDATE bets SET ts=?, model_p=?, price=?, edge=?, stake=?,
                   contracts=?, rationale=?, sharp=?, game=? WHERE id=?""",
                (now, rec.model_p, rec.implied, rec.edge, rec.stake,
                 rec.contracts, rationale or rec.rationale, rec.sharp_p, game, row[0]))
            c.commit()
            return row[0]
    cur = c.execute(
        """INSERT INTO bets (ts, mode, market, ticker, model_p, price, edge,
           stake, contracts, confidence, rationale, key_factors, side, sharp,
           placed, game)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now, mode, rec.market, rec.ticker, rec.model_p, rec.implied, rec.edge,
         rec.stake, rec.contracts, confidence, rationale or rec.rationale,
         json.dumps(key_factors or []), rec.side, rec.sharp_p, 1 if placed else 0,
         game))
    c.commit()
    return cur.lastrowid


def settle(bet_id: int, won: bool, close_price: Optional[float] = None,
           fee: float = 0.0):
    c = _conn()
    row = c.execute("SELECT price, contracts, stake FROM bets WHERE id=?",
                    (bet_id,)).fetchone()
    if not row:
        raise ValueError(f"bet {bet_id} not found")
    price, contracts, stake = row
    pnl = contracts * (1 - price) - fee if won else -(stake + fee)
    c.execute("""UPDATE bets SET status=?, pnl=?, close_price=?, settled_ts=?, fee=?
                 WHERE id=?""",
              ("won" if won else "lost", round(pnl, 2), close_price,
               datetime.now(timezone.utc).isoformat(), round(fee, 4), bet_id))
    c.commit()
    from . import lessons
    try:
        lessons.add_lesson(bet_id)
    except Exception as e:  # noqa: BLE001 - don't let lesson-writing break settlement
        print(f"  (lesson generation failed: {e})")


def auto_settle_from_kalshi() -> list[dict]:
    """Check Kalshi's settlement history and settle any matching open placed bets.

    Matches by ticker; `market_result` ('yes'/'no') vs the bet's `side` decides
    won/lost. Closing price is recorded as 1.0 (won) or 0.0 (lost) — a settled
    contract pays out at $1 or $0.
    """
    from .kalshi import KalshiTradeClient
    c = _conn()
    rows = c.execute(
        "SELECT id, ticker, side, market FROM bets "
        "WHERE status='open' AND placed=1").fetchall()
    if not rows:
        return []
    settlements = {s["ticker"]: s for s in KalshiTradeClient().settlements()}
    out = []
    for bet_id, ticker, side, market in rows:
        s = settlements.get(ticker)
        if not s:
            continue
        won = s.get("market_result") == side
        fee = float(s.get("fee_cost") or 0)
        settle(bet_id, won, close_price=1.0 if won else 0.0, fee=fee)
        out.append({"bet_id": bet_id, "market": market, "ticker": ticker,
                    "won": won, "fee": fee})
    return out


def record_analysis(home: str, away: str, summary: str, n_rec: int, model: str = "",
                    prompt_snapshot: dict | None = None):
    """Log that a match was analyzed (preserves any existing placed count)."""
    from . import llm
    c = _conn()
    key = _game_key(home, away)
    prev = c.execute("SELECT n_placed FROM analyses WHERE game=?", (key,)).fetchone()
    c.execute("INSERT OR REPLACE INTO analyses (game, ts, summary, n_rec, n_placed, model, prompt_snapshot) "
              "VALUES (?,?,?,?,?,?,?)",
              (key, datetime.now(timezone.utc).isoformat(), summary, n_rec,
               prev[0] if prev else 0, model or llm.model_label(),
               json.dumps(prompt_snapshot) if prompt_snapshot else None))
    c.commit()


def bump_placed(home: str, away: str, n: int):
    c = _conn()
    c.execute("UPDATE analyses SET n_placed = n_placed + ? WHERE game=?",
              (n, _game_key(home, away)))
    c.commit()


def pnl_for(home: str, away: str) -> dict | None:
    """Settled P/L (+ ROI) for this match's placed bets, or None if none are placed."""
    rows = _conn().execute(
        "SELECT pnl, status, stake FROM bets WHERE game=? AND placed=1",
        (_game_key(home, away),)).fetchall()
    if not rows:
        return None
    settled = [(r[0], r[2]) for r in rows if r[1] in ("won", "lost")]
    pnl = round(sum(p for p, _ in settled), 2) if settled else None
    stake = sum(s for _, s in settled)
    return {
        "pnl": pnl,
        "roi": round(pnl / stake * 100, 1) if settled and stake else None,
        "n_settled": len(settled),
        "n_open": sum(1 for r in rows if r[1] == "open"),
    }


def analysis_for(home: str, away: str) -> dict | None:
    r = _conn().execute(
        "SELECT ts, summary, n_rec, n_placed, model FROM analyses WHERE game=?",
        (_game_key(home, away),)).fetchone()
    if not r:
        return None
    out = {"ts": r[0], "summary": r[1], "n_rec": r[2], "n_placed": r[3], "model": r[4]}
    pnl = pnl_for(home, away)
    if pnl:
        out.update(pnl)
    return out


def settled_bets_for_team(team: str) -> list[dict]:
    """All settled placed bets for games involving this team, newest first."""
    from .teams import canon_team
    canon = canon_team(team)
    rows = _conn().execute(
        "SELECT game, market, side, model_p, price, status, pnl, settled_ts "
        "FROM bets WHERE status IN ('won','lost') AND placed=1 "
        "ORDER BY settled_ts"
    ).fetchall()
    out = []
    for game, market, side, model_p, price, status, pnl, ts in rows:
        parts = game.split("|")
        if not any(canon_team(p) == canon for p in parts):
            continue
        opponent = next((p for p in parts if canon_team(p) != canon), "?")
        out.append({
            "opponent": opponent, "market": market, "side": side,
            "model_p": round(model_p, 4), "price": round(price, 4),
            "won": status == "won", "pnl": round(pnl, 2),
        })
    return out


def open_bets(placed_only: bool = False) -> list:
    q = ("SELECT id, market, side, stake, model_p, price, placed FROM bets "
         "WHERE status='open'")
    if placed_only:
        q += " AND placed=1"
    return _conn().execute(q).fetchall()


def futures_bets() -> dict:
    """Open futures recommendations/placed bets, keyed by ticker."""
    rows = _conn().execute(
        "SELECT ticker, market, side, model_p, price, sharp, edge, stake, "
        "contracts, placed FROM bets WHERE mode='futures' AND status='open'"
    ).fetchall()
    cols = ["ticker", "market", "side", "model_p", "price", "sharp", "edge",
            "stake", "contracts", "placed"]
    return {r[0]: dict(zip(cols, r)) for r in rows}


def kill_switch() -> tuple[bool, str]:
    """(tripped, reason). Trips when settled PLACED bets show we're behind."""
    rows = _conn().execute(
        "SELECT model_p, price, status, close_price FROM bets "
        "WHERE status IN ('won','lost') AND placed=1").fetchall()
    n = len(rows)
    if n < config.KILL_SWITCH_MIN_BETS:
        return False, f"active ({n}/{config.KILL_SWITCH_MIN_BETS} settled bets)"
    clv = [(r[3] - r[1]) for r in rows if r[3] is not None]
    avg_clv = sum(clv) / len(clv) if clv else None
    brier_model = sum((r[0] - (1 if r[2] == "won" else 0)) ** 2 for r in rows) / n
    brier_mkt = sum((r[1] - (1 if r[2] == "won" else 0)) ** 2 for r in rows) / n
    if avg_clv is not None and avg_clv < 0:
        return True, f"TRIPPED: avg CLV {avg_clv:+.3f} over {n} bets — behind the close"
    if brier_model > brier_mkt:
        return True, (f"TRIPPED: model Brier {brier_model:.3f} worse than market "
                      f"{brier_mkt:.3f} over {n} bets")
    return False, f"healthy over {n} settled bets"


def summary() -> str:
    c = _conn()
    rows = c.execute(
        "SELECT model_p, price, status, pnl, close_price FROM bets "
        "WHERE status IN ('won','lost') AND placed=1").fetchall()
    n_rec = c.execute(
        "SELECT COUNT(*) FROM bets WHERE placed=0").fetchone()[0]
    n_open = c.execute(
        "SELECT COUNT(*) FROM bets WHERE status='open' AND placed=1").fetchone()[0]
    head = f"Open placed bets: {n_open} | recommendations logged: {n_rec}"
    if not rows:
        return head + "\nNo settled placed bets yet."
    n = len(rows)
    wins = sum(1 for r in rows if r[2] == "won")
    pnl = sum(r[3] or 0 for r in rows)
    brier_model = sum((r[0] - (1 if r[2] == "won" else 0)) ** 2 for r in rows) / n
    brier_mkt = sum((r[1] - (1 if r[2] == "won" else 0)) ** 2 for r in rows) / n
    clv = [(r[4] - r[1]) for r in rows if r[4] is not None]
    avg_clv = sum(clv) / len(clv) if clv else None
    total_stake = sum(s[0] or 0 for s in c.execute(
        "SELECT stake FROM bets WHERE status IN ('won','lost') AND placed=1"
    ).fetchall())
    roi = pnl / total_stake if total_stake else 0
    lines = [head,
             f"Settled: {n} | record {wins}-{n - wins} | P/L ${pnl:+.2f} "
             f"| ROI {roi:+.1%}",
             f"Brier — model {brier_model:.3f} vs market {brier_mkt:.3f} "
             f"({'model sharper' if brier_model < brier_mkt else 'market sharper'})"]
    if avg_clv is not None:
        lines.append(f"Avg CLV: {avg_clv:+.3f} "
                     f"({'beating' if avg_clv > 0 else 'behind'} the close)")
    tripped, reason = kill_switch()
    lines.append(f"Kill-switch: {reason}")
    return "\n".join(lines)


def review_reasoning() -> str:
    """Feed settled bets (reasoning + outcome) to the model to extract lessons."""
    rows = _conn().execute(
        "SELECT market, model_p, price, status, pnl, confidence, rationale "
        "FROM bets WHERE status IN ('won','lost') ORDER BY settled_ts").fetchall()
    if len(rows) < 5:
        return f"Only {len(rows)} settled bets — need ~5+ for a meaningful review."

    from . import llm
    cases = "\n\n".join(
        f"BET: {r[0]}\n  model {r[1]:.0%} vs market {r[2]:.0%} | conf {r[5]} "
        f"| RESULT {r[3].upper()} (P/L ${r[4]:+.2f})\n  reasoning: {r[6]}"
        for r in rows)
    sys = ("You are auditing a football betting model's reasoning. You are given "
           "past bets with the reasoning used and whether they won or lost. "
           "Identify patterns that distinguish GOOD reasoning (led to wins / "
           "positive value) from BAD reasoning (led to losses), and propose 3-6 "
           "concrete, reusable lessons to add to the prediction prompt. Be "
           "specific about systematic biases (e.g. overrating home form, "
           "underrating fatigue). Note: variance is real — judge the reasoning "
           "process, not just the single outcome.")
    user = (f"Here are {len(rows)} settled bets:\n\n{cases}\n\n"
            "What separates the good from the bad reasoning, and what "
            "lessons should we add to the prompt?")
    return llm.complete_text(sys, user, max_tokens=4000, effort="high")
