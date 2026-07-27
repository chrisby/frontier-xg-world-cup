"""Incremental learning loop: one lesson per settled game (covering all bets).

Each time all bets for a game are settled, the reasoning model writes a single
reusable takeaway that considers every market bet on that game together. All
accumulated lessons are prepended to the match-reasoning system prompt.
"""
import json
from datetime import datetime, timezone

from . import config, llm

LESSONS_FILE = config.DATA_DIR / "lessons.json"

_SYSTEM = (
    "You are auditing all settled bets on a single football match. Given each "
    "market, the model's probability, the market price, the outcome, and the "
    "final score, write ONE short, reusable lesson (2-3 sentences, max ~60 words) "
    "that covers the most important pattern across all these bets.\n"
    "- If bets mostly WON: phrase as positive reinforcement ('Confirmed:...', "
    "'When X, Y holds:...').\n"
    "- If bets mostly LOST: phrase as a warning ('Watch out for...', "
    "'Don't overweight...').\n"
    "- If mixed: capture the key tension ('X held but Y didn't because...').\n"
    "Phrased as a general rule applicable to future matches, not a recap of "
    "this specific game. If results were just normal variance, say so."
)


def _load() -> list:
    if LESSONS_FILE.exists():
        return json.loads(LESSONS_FILE.read_text())
    return []


def _save(lessons: list):
    LESSONS_FILE.write_text(json.dumps(lessons, indent=1))


def _lookup_score(game: str) -> str | None:
    """Return 'TeamA N-M TeamB' final score for a WC 2026 game key, or None."""
    try:
        from .sportmonks import SportMonks
        from .publish import _final_score
        from .teams import canon_team
        teams = set(game.split("|"))
        sm = SportMonks()
        for f in sm.wc_fixtures():
            name = (f.get("name") or "")
            parts = name.split(" vs ")
            if len(parts) != 2:
                continue
            fixture_teams = {canon_team(parts[0].strip()), canon_team(parts[1].strip())}
            if teams == fixture_teams:
                s = _final_score(f)
                if s:
                    return f"{parts[0].strip()} {s['home']}-{s['away']} {parts[1].strip()}"
    except Exception:
        pass
    return None


def add_game_lesson(game: str) -> dict | None:
    """Distill one lesson from all settled bets on a game, append to
    lessons.json (deduped by game key). Returns the new entry, or None if
    no settled match bets exist for the game."""
    from . import ledger
    rows = ledger._conn().execute(
        "SELECT id, market, model_p, price, status, pnl, confidence, rationale "
        "FROM bets WHERE game=? AND mode='match' AND status IN ('won','lost') "
        "ORDER BY id", (game,)
    ).fetchall()
    if not rows:
        return None

    lessons = _load()
    if any(l.get("game") == game and "bets" in l for l in lessons):
        return None

    score = _lookup_score(game)

    bets = []
    bet_lines = []
    for bet_id, market, model_p, price, status, pnl, confidence, rationale in rows:
        bets.append({
            "bet_id": bet_id, "market": market, "outcome": status, "pnl": round(pnl, 2),
        })
        won_lost = "WON" if status == "won" else "LOST"
        bet_lines.append(
            f"  Market: {market}\n"
            f"  Model prob: {model_p:.0%} | Market price: {price:.0%} | "
            f"Confidence: {confidence} | Outcome: {won_lost} (P/L ${pnl:+.2f})\n"
            f"  Reasoning: {rationale}"
        )

    score_line = f"Final score: {score}\n\n" if score else ""
    user = (
        f"Game: {game.replace('|', ' vs ')}\n"
        f"{score_line}"
        f"Bets placed ({len(bets)}):\n\n"
        + "\n\n".join(bet_lines)
        + "\n\nWhat's the one reusable lesson across all these bets?"
    )
    text = llm.complete_text(_SYSTEM, user, max_tokens=400, effort="low").strip()

    entry = {
        "game": game,
        "score": score,
        "ts": datetime.now(timezone.utc).isoformat(),
        "bets": bets,
        "lesson": text,
    }
    lessons.append(entry)
    _save(lessons)
    return entry


def add_lesson(bet_id: int) -> dict | None:
    """Called by ledger.settle() after each bet settles. Triggers a game-level
    lesson once ALL bets for that game are settled."""
    from . import ledger
    row = ledger._conn().execute(
        "SELECT mode, game FROM bets WHERE id=?", (bet_id,)
    ).fetchone()
    if not row or row[0] != "match":
        return None
    game = row[1]

    # Only write the lesson once all placed bets for this game are settled
    open_count = ledger._conn().execute(
        "SELECT COUNT(*) FROM bets WHERE game=? AND mode='match' AND placed=1 "
        "AND status='open'", (game,)
    ).fetchone()[0]
    if open_count > 0:
        return None

    return add_game_lesson(game)


def prompt_block() -> str:
    """A short 'lessons learned' section to prepend to the reasoning prompt,
    or '' if nothing's been settled yet."""
    lessons = _load()
    if not lessons:
        return ""
    bullets = "\n".join(f"- {l['lesson']}" for l in lessons)
    return (
        "Lessons learned from past settled bets (apply these where relevant):\n"
        f"{bullets}\n\n"
    )
