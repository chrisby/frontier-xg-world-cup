"""Unified match schedule.

SportMonks is authoritative for group stage fixtures (it has full metadata).
Once the knockout phase begins, Kalshi's open KXWCGAME markets are used as a
fallback for any game not already in the SportMonks schedule — Kalshi lists
R16+ fixtures as soon as the bracket is set, before SportMonks catches up.

All callers should use `get_schedule(sm)` instead of `sm.schedule()` directly.
"""
from collections import defaultdict

from . import config
from .teams import canon_team


def _pair_key(a: str, b: str) -> frozenset:
    return frozenset({canon_team(a), canon_team(b)})


def _is_placeholder_name(name: str) -> bool:
    """True for unresolved bracket slots like 'Winner Match 77' or '2nd Group G'."""
    n = (name or "").strip()
    return (not n) or n[0].isdigit() or n.startswith("Winner") or n.startswith("Loser") or "Group" in n


def _sportmonks_games(sm) -> list[dict]:
    out = []
    for f in sm.schedule():
        if f.get("placeholder") or not f.get("start"):
            continue
        teams = [t.strip() for t in f["name"].split(" vs ")]
        out.append({
            "name":        f["name"],
            "home":        teams[0] if teams else "",
            "away":        teams[1] if len(teams) > 1 else "",
            "start":       f["start"],
            "stage":       f.get("stage", "Group Stage"),
            "placeholder": False,
            "source":      "sportmonks",
        })
    return out


def _kalshi_games() -> list[dict]:
    """Derive upcoming fixtures from open Kalshi KXWCGAME markets."""
    try:
        from .kalshi import KalshiReadClient
        k = KalshiReadClient()
        markets = k.markets(series_ticker=config.KALSHI_SERIES["match"], status="open")
    except Exception:
        return []

    # Group by event — each event has 3 markets (home/tie/away)
    by_event: dict[str, list] = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker", "")].append(m)

    out = []
    for ev, ms in by_event.items():
        # Extract kickoff from occurrence_datetime on any market in the event
        occurrence = next(
            (m.get("occurrence_datetime") for m in ms if m.get("occurrence_datetime")),
            None,
        )
        if not occurrence:
            continue

        # Convert "2026-07-02T03:00:00Z" → "2026-07-02 03:00:00"
        start = occurrence.replace("T", " ").replace("Z", "")

        # Extract team names from the title: "USA vs Bosnia and Herzegovina Winner?"
        title = ms[0].get("title", "")
        name = title.replace(" Winner?", "").strip()
        teams = [t.strip() for t in name.split(" vs ")]
        if len(teams) != 2:
            continue

        # Stage is overridden by SportMonks in get_schedule(); this is a fallback only
        if start[:10] <= "2026-06-27":
            stage = "Group Stage"
        else:
            stage = "Knockout"  # will be replaced by SportMonks stage name

        out.append({
            "name":        name,
            "home":        teams[0],
            "away":        teams[1],
            "start":       start,
            "stage":       stage,
            "placeholder": False,
            "source":      "kalshi",
        })
    return out


def get_schedule(sm) -> list[dict]:
    """Unified schedule: SportMonks group stage + Kalshi knockout games."""
    fixtures = _sportmonks_games(sm)
    seen = {_pair_key(f["home"], f["away"]) for f in fixtures}

    # Build a kickoff lookup from wc_fixtures() — SportMonks has accurate
    # kickoff times for knockout games even when sm.schedule() omits them.
    # Kalshi's occurrence_datetime is the market resolution time (~3h post-kickoff).
    # Also use wc_fixtures() to recover completed knockout games whose Kalshi
    # markets have already settled (and so no longer appear in open markets).
    sm_kickoffs: dict[frozenset, str] = {}
    sm_stages: dict[frozenset, str] = {}   # authoritative stage from SportMonks
    sm_ko_completed: list[dict] = []
    try:
        for f in sm.wc_fixtures():
            parts = f.get("participants", [])
            if len(parts) != 2:
                continue
            a, b = parts[0].get("name", ""), parts[1].get("name", "")
            start = (f.get("starting_at") or "")[:19]
            if not start:
                continue
            key = _pair_key(a, b)
            sm_kickoffs[key] = start
            # Use SportMonks stage name directly — more reliable than date inference
            sm_stage = (f.get("stage") or {}).get("name", "")
            if sm_stage:
                sm_stages[key] = sm_stage
            # All knockout games (completed or upcoming) not yet in seen
            if start[:10] > "2026-06-27" and key not in seen:
                is_placeholder = _is_placeholder_name(a) or _is_placeholder_name(b)
                fixture_name = f.get("name") or f"{a} vs {b}"
                teams = [t.strip() for t in fixture_name.split(" vs ")]
                home = teams[0] if len(teams) == 2 else a
                away = teams[1] if len(teams) == 2 else b
                stage = sm_stage or ("Round of 32" if start[:10] <= "2026-07-03" else "Round of 16")
                sm_ko_completed.append({
                    "name":        fixture_name,
                    "home":        home,
                    "away":        away,
                    "start":       start,
                    "stage":       stage,
                    "placeholder": is_placeholder,
                    "source":      "sportmonks",
                })
                seen.add(key)
    except Exception:
        pass

    fixtures.extend(sm_ko_completed)

    for g in _kalshi_games():
        key = _pair_key(g["home"], g["away"])
        if key not in seen:
            if key in sm_kickoffs:
                g["start"] = sm_kickoffs[key]
            if key in sm_stages:
                g["stage"] = sm_stages[key]
            fixtures.append(g)
            seen.add(key)

    return sorted(fixtures, key=lambda f: f["start"])
