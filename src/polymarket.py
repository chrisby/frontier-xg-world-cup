"""Polymarket Gamma client — the "sharp line" anchor.

Polymarket's WC books are far deeper than Kalshi's (winner event ~$1.9B volume,
match events ~$1.6M each, Golden Boot ~$4.8M). We never trade there; we read its
prices as a high-quality estimate of the truth and use them to gate our own
bets: if our model and Polymarket disagree about the direction of a Kalshi
mispricing, the most likely explanation is that our model is wrong.

Public API, no auth. Prices are the bid/ask midpoint of the YES outcome.
"""
import time

import requests

from . import config
from .teams import same_team, team_in_text

_TTL = 600  # seconds
_cache: dict = {}


def _get(path: str, params: dict | None = None) -> list | dict:
    key = (path, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    r = requests.get(f"{config.POLYMARKET_GAMMA}{path}", params=params or {},
                     timeout=25)
    r.raise_for_status()
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


def _mid(m: dict) -> float | None:
    bid, ask = m.get("bestBid"), m.get("bestAsk")
    if bid is not None and ask is not None and 0 < ask < 1:
        return (float(bid) + float(ask)) / 2
    try:  # fall back to the listed outcome price
        p = float((m.get("outcomePrices") or ["", ""])[0])
        return p if 0 < p < 1 else None
    except (ValueError, TypeError, IndexError):
        return None


def winner_probs() -> dict:
    """{team_name: prob} from the deep world-cup-winner event."""
    try:
        evs = _get("/events", {"slug": "world-cup-winner"})
    except Exception:
        return {}
    out = {}
    for m in (evs[0].get("markets", []) if evs else []):
        name, p = m.get("groupItemTitle"), _mid(m)
        if name and p is not None:
            out[name] = p
    return out


def golden_boot_probs() -> dict:
    """{player_name: prob} from the Golden Boot event."""
    try:
        evs = _get("/events", {"slug": "world-cup-golden-boot-winner"})
    except Exception:
        return {}
    out = {}
    for m in (evs[0].get("markets", []) if evs else []):
        name, p = m.get("groupItemTitle"), _mid(m)
        if name and p is not None:
            out[name] = p
    return out


def match_probs(home: str, away: str) -> dict:
    """{'home','draw','away'} sharp probs for a match, {} if not listed.

    Found via search (slugs like fifwc-kr-cze-2026-06-11 aren't derivable).
    """
    def _find(query: str):
        try:
            res = _get("/public-search", {"q": query, "limit_per_type": 12})
        except Exception:
            return None
        for e in res.get("events", []):
            title = e.get("title") or ""
            if " vs" not in title.lower():
                continue
            parts = title.replace(" vs. ", " vs ").split(" vs ")
            if len(parts) == 2 and (
                (same_team(parts[0], home) and same_team(parts[1], away))
                or (same_team(parts[0], away) and same_team(parts[1], home))
            ):
                return e
        return None

    # try query variants — Polymarket's names differ (Czechia vs Czech Republic)
    event = None
    for q in (f"{home} {away}", home, away):
        event = _find(q)
        if event:
            break
    if not event:
        return {}
    try:
        evs = _get("/events", {"slug": event.get("slug")})
    except Exception:
        return {}
    out = {}
    for m in (evs[0].get("markets", []) if evs else []):
        g = m.get("groupItemTitle") or ""
        p = _mid(m)
        if p is None:
            continue
        if "draw" in g.lower():
            out["draw"] = p
        elif team_in_text(g, home):
            out["home"] = p
        elif team_in_text(g, away):
            out["away"] = p
    return out


def lookup(name: str, probs: dict) -> float | None:
    """Find a team/player in a {name: prob} map with fuzzy name matching."""
    for k, v in probs.items():
        if same_team(name, k) or team_in_text(k, name) or team_in_text(name, k):
            return v
    return None
