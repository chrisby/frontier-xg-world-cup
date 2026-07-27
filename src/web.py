"""Flask backend for the betting-desk web UI.

Wraps the orchestrator scripts as endpoints. Read endpoints (state/schedule)
are cheap; analyze triggers a reasoning pass; place submits signed orders.
Run: python -m src.web   (then open http://127.0.0.1:5000)
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request

from . import config, ledger, orchestrator
from .kalshi import KalshiReadClient, live_bankroll
from .sportmonks import SportMonks

app = Flask(__name__)


# Stale-while-revalidate cache of which games have a live book. Checking every
# game's order book is ~72 calls, too slow to do on each /api/state — so we serve
# the last-known set instantly and refresh it in the background.
_PRICED = {"pairs": set(), "ts": 0.0, "running": False}
_PRICED_TTL = 150


def _compute_priced_pairs():
    from .teams import canon_team
    try:
        k = KalshiReadClient()
        ms = k.markets(series_ticker=config.KALSHI_SERIES["match"], status="open")
        games: dict = {}
        for m in ms:
            tt = (m.get("title") or "").replace(" Winner?", "").split(" vs ")
            if len(tt) != 2:
                continue
            pair = frozenset({canon_team(tt[0]), canon_team(tt[1])})
            games.setdefault(pair, []).append(m.get("ticker"))
        out = set()
        for pair, tickers in games.items():
            for tk in tickers:
                if tk and k.book_prices(tk).get("yes_ask") is not None:
                    out.add(pair)
                    break
        _PRICED["pairs"] = out
        _PRICED["ts"] = time.time()
    finally:
        _PRICED["running"] = False


def _priced_pairs() -> set:
    """Canonical team-pairs with a live Kalshi book (all games), cached."""
    if time.time() - _PRICED["ts"] > _PRICED_TTL and not _PRICED["running"]:
        _PRICED["running"] = True
        threading.Thread(target=_compute_priced_pairs, daemon=True).start()
    return _PRICED["pairs"]


def _phase(delta: float) -> str:
    """Timing bucket from seconds-to-kickoff -> what the user should do."""
    if delta < -8000:
        return "finished"
    if delta < 0:
        return "live"
    if delta < 2 * 3600:
        return "run_now"      # lineups out — analyze + place pre-kickoff
    if delta < 26 * 3600:
        return "analyze"      # within a day — analyze today
    return "scheduled"


_INDEX = Path(__file__).resolve().parent.parent / "webapp" / "index.html"


@app.route("/")
def index():
    return Response(_INDEX.read_text(), mimetype="text/html")


@app.route("/api/state")
def state():
    from .teams import canon_team
    sm = SportMonks()
    priced = _priced_pairs()
    now = datetime.now(timezone.utc)
    games = []
    from .schedule import get_schedule
    for f in get_schedule(sm):
        if not f.get("start"):
            continue
        try:
            ko = datetime.strptime(f["start"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        delta = (ko - now).total_seconds()
        teams = [t.strip() for t in f["name"].split(" vs ")]
        is_placeholder = f.get("placeholder", False)
        games.append({
            "name": f["name"],
            "home": teams[0] if teams else "",
            "away": teams[1] if len(teams) > 1 else "",
            "stage": f["stage"],
            "kickoff": ko.isoformat(),
            "delta": delta,
            "phase": "placeholder" if is_placeholder else _phase(delta),
            "priced": False if is_placeholder else frozenset({canon_team(teams[0]),
                                 canon_team(teams[1] if len(teams) > 1 else "")}) in priced,
            "analysis": None if is_placeholder else ledger.analysis_for(teams[0], teams[1] if len(teams) > 1 else ""),
        })
    games.sort(key=lambda g: g["kickoff"])
    bal = live_bankroll()
    upcoming = [g for g in games if g["phase"] in ("run_now", "analyze")]
    tripped, reason = ledger.kill_switch()
    from . import team_rating
    team_rating.sync_refresh_flags(sm)  # cheap: wc_results() is cached 12min
    needs_refresh = team_rating.teams_needing_refresh()
    from . import llm
    return jsonify({
        "now": now.isoformat(),
        "balance": bal,
        "bankroll": bal if bal is not None else config.BANKROLL_USD,
        "model": llm.model_label(),
        "games": games,
        "actions": {
            "run_now": [g["name"] for g in games if g["phase"] == "run_now"],
            "today": [g["name"] for g in upcoming if g["phase"] == "analyze"],
        },
        "ledger": ledger.summary(),
        "health": {"tripped": tripped, "reason": reason},
        "needs_refresh": needs_refresh,
    })


@app.route("/api/dossier")
def dossier():
    """Return the last-published dossier (reasoning + bets + no_bets) for a
    match, if one has ever been analyzed — without re-running the model."""
    from . import publish as _pub
    home, away = request.args.get("home", ""), request.args.get("away", "")
    f = _pub.DOSS / f"{_pub.slug(home, away)}.json"
    if not f.exists():
        return jsonify({"ok": False})
    data = json.loads(f.read_text())
    placed = ledger.placed_tickers(ledger.game_key(home, away))
    n_unplaced = sum(1 for b in data.get("bets", [])
                     if (b["ticker"], b["side"]) not in placed)
    return jsonify({"ok": True, "result": data, "n_unplaced": n_unplaced})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    d = request.get_json(force=True)
    home, away = d["home"], d["away"]
    try:
        text = orchestrator.run_match(home, away, record=False)
        recs = orchestrator._LAST_RECS.get((home, away), [])
        return jsonify({"ok": True, "text": text, "n_bets": len(recs),
                        "result": orchestrator._LAST_RESULT.get((home, away))})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "text": f"Error: {e}"}), 500


@app.route("/api/place", methods=["POST"])
def place():
    d = request.get_json(force=True)
    results = orchestrator.place_cached(d["home"], d["away"])
    return jsonify({"ok": True, "results": results})


_FUTURES = {"status": "idle", "text": ""}  # async build state
_RATINGS_FILE = config.DATA_DIR / "ratings.json"
TOTAL_TEAMS = 48


def _rated_count() -> int:
    try:
        return len(json.loads(_RATINGS_FILE.read_text()))
    except Exception:
        return 0


def _build_active() -> bool:
    """True if a rating build (UI thread OR a CLI run) is actively writing."""
    if _FUTURES["status"] == "running":
        return True
    return (_RATINGS_FILE.exists()
            and time.time() - _RATINGS_FILE.stat().st_mtime < 90
            and _rated_count() < TOTAL_TEAMS)


def _run_futures_bg():
    _FUTURES["status"] = "running"
    try:
        text = orchestrator.run_futures_bets()
        from . import publish
        pub = publish.publish_futures()
        _FUTURES["text"] = text + "\n\n" + pub
        _FUTURES["status"] = "done"
    except Exception as e:  # noqa: BLE001
        _FUTURES["text"] = f"Error: {e}"
        _FUTURES["status"] = "error"


@app.route("/api/futures", methods=["POST"])
def futures():
    """Kick off the build in the background; return immediately."""
    if not _build_active():
        _FUTURES["text"] = ""
        threading.Thread(target=_run_futures_bg, daemon=True).start()
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/futures/progress")
def futures_progress():
    rated = _rated_count()
    status = _FUTURES["status"]
    if status == "idle" and _build_active():
        status = "running"   # a CLI build is running; reflect it
    return jsonify({"status": status, "rated": rated, "total": TOTAL_TEAMS,
                    "text": _FUTURES["text"]})


_DEPLOY = {"status": "idle", "text": ""}


@app.route("/api/deploy", methods=["POST"])
def deploy():
    """Placeholder — wire up your own deploy script via DEPLOY_CMD env var or override this endpoint."""
    deploy_cmd = os.environ.get("DEPLOY_CMD", "")
    if not deploy_cmd:
        return jsonify({"ok": False, "status": "error",
                        "text": "No DEPLOY_CMD configured. Set DEPLOY_CMD in your .env to a script that builds and pushes your site."})
    import subprocess
    _DEPLOY["status"] = "running"
    _DEPLOY["text"] = ""
    def _run():
        try:
            r = subprocess.run(deploy_cmd, shell=True, capture_output=True, text=True, timeout=600)
            _DEPLOY["text"] = (r.stdout + r.stderr)[-4000:]
            _DEPLOY["status"] = "done" if r.returncode == 0 else "error"
        except Exception as e:  # noqa: BLE001
            _DEPLOY["text"] = f"Error: {e}"
            _DEPLOY["status"] = "error"
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/deploy/progress")
def deploy_progress():
    return jsonify({"status": _DEPLOY["status"], "text": _DEPLOY["text"]})


@app.route("/api/check-settlements", methods=["POST"])
def check_settlements():
    try:
        results = ledger.auto_settle_from_kalshi()
        from . import publish
        publish.publish_site()
        if results:
            lines = "\n".join(
                f"  {'✓ won ' if r['won'] else '✗ lost'} {r['market']}"
                for r in results)
            text = f"Settled {len(results)} bet(s) from Kalshi:\n{lines}"
        else:
            text = "No new settlements found on Kalshi."
        return jsonify({"ok": True, "n": len(results), "text": text})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "text": f"Error: {e}"}), 500


@app.route("/api/publish", methods=["POST"])
def publish_site():
    try:
        from . import publish
        publish.publish_site()
        return jsonify({"ok": True, "text": "Published site.json."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "text": f"Error: {e}"}), 500


if __name__ == "__main__":
    # port 8000 (not 5000 — macOS AirPlay Receiver squats on 5000 and returns 403)
    app.run(host="127.0.0.1", port=8000, debug=False)
