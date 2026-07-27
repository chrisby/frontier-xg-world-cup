"""Reasoned per-team strength ratings for the tournament simulator.

Same philosophy as the match reasoner: Fable 5 reads a team's dossier and outputs
its strength as two expected-goal numbers (vs an average World Cup team), NOT a
hardcoded formula. Ratings are cached to disk so the 48-team build runs once.
"""
import json

from . import config, llm
from .dossier import build_team_dossier, render_markdown
from .sportmonks import SportMonks

_CACHE = config.DATA_DIR / "ratings.json"

# expected goals for/against vs an average WC team; ~1.3 is league-average
LEAGUE_AVG = 1.3

_SYSTEM = """You are a world-class football analyst rating one nation's strength \
for the 2026 World Cup, independent of any specific opponent.

From the dossier, output two numbers:
- off_rating: the goals this team would be expected to SCORE against an AVERAGE \
2026 World Cup team (the average side rates ~1.3). Strong attacks ~1.8-2.4, weak \
~0.6-1.0.
- def_rating: the goals this team would be expected to CONCEDE against an AVERAGE \
2026 World Cup team (average ~1.3). Strong defences ~0.7-1.0, leaky ~1.7-2.4.

Rules:
- Reason bottom-up from squad quality, coach, form (weighting opponent quality — \
big wins over weak qualifying sides mean little), and tournament pedigree. Do NOT \
use or guess betting odds.
- The field of 48 ranges from genuine contenders (Argentina, France, Spain, \
Brazil, England) to minnows. Spread your ratings to reflect that real gap.
- Be calibrated and self-critical about what the data shows."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "off_rating": {"type": "number"},
        "def_rating": {"type": "number"},
        "tier": {"type": "string",
                 "enum": ["contender", "dark_horse", "solid", "weak"]},
        "rationale": {"type": "string"},
    },
    "required": ["off_rating", "def_rating", "tier", "rationale"],
    "additionalProperties": False,
}


def _load_cache() -> dict:
    if _CACHE.exists():
        return json.loads(_CACHE.read_text())
    return {}


def _save_cache(c: dict):
    _CACHE.write_text(json.dumps(c, indent=1))


def _played_counts(results: dict) -> dict:
    """{team_name: number of finished WC games} from sm.wc_results()."""
    counts: dict = {}
    for key in list(results.get("group", {})) + list(results.get("ko", {})):
        for name in key:
            counts[name] = counts.get(name, 0) + 1
    return counts


def sync_refresh_flags(sm: SportMonks) -> list[str]:
    """Flag cached teams that have played a new WC game since their last
    rating (form/lineup is now stale). Returns names newly flagged."""
    cache = _load_cache()
    counts = _played_counts(sm.wc_results())
    newly = []
    for name, entry in cache.items():
        if counts.get(name, 0) > entry.get("wc_games_seen", 0) and not entry.get("needs_refresh"):
            entry["needs_refresh"] = True
            newly.append(name)
    if newly:
        _save_cache(cache)
    return newly


def teams_needing_refresh() -> list[str]:
    return [n for n, e in _load_cache().items() if e.get("needs_refresh")]


def refresh_played_teams(sm: SportMonks, name_to_id: dict) -> list[str]:
    """Re-rate (refresh=True) any team flagged by sync_refresh_flags, then
    clear the flag. Returns the names actually refreshed."""
    sync_refresh_flags(sm)
    counts = _played_counts(sm.wc_results())
    done = []
    for name in teams_needing_refresh():
        tid = name_to_id.get(name)
        if not tid:
            continue
        rate_team(sm, tid, name, refresh=True)
        cache = _load_cache()
        cache[name]["wc_games_seen"] = counts.get(name, 0)
        cache[name]["needs_refresh"] = False
        _save_cache(cache)
        done.append(name)
    return done


_SHARES = config.DATA_DIR / "shares.json"


def _cache_shares(name: str, dossier: dict):
    """Persist per-player goal shares (goals / team total) for the Golden Boot sim."""
    players = {p["name"]: (p.get("goals") or 0) for p in dossier.get("players", [])
               if p.get("name") and (p.get("goals") or 0) > 0}
    total = sum(players.values())
    if total <= 0:
        return
    shares = json.loads(_SHARES.read_text()) if _SHARES.exists() else {}
    shares[name] = {p: round(g / total, 4) for p, g in players.items()}
    _SHARES.write_text(json.dumps(shares, indent=1))


def rate_team(sm: SportMonks, team_id: int, name: str,
              refresh: bool = False) -> dict:
    cache = _load_cache()
    if not refresh and name in cache:
        return cache[name]

    dossier = build_team_dossier(sm, team_id)
    _cache_shares(name, dossier)  # player goal-shares for the Golden Boot sim
    brief = render_markdown(dossier)
    out = llm.complete_json(
        _SYSTEM, f"Rate {name} for the 2026 World Cup.\n\n{brief}",
        _SCHEMA, max_tokens=8000, effort="high")
    out["dossier"] = brief  # store the exact input the model reasoned over
    out["model"] = llm.model_label()
    cache[name] = out
    _save_cache(cache)
    return out
