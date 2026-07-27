"""Resolve nation names (as Kalshi labels them) to SportMonks team IDs."""
from .sportmonks import SportMonks

# Kalshi label -> SportMonks name, where they differ
_ALIASES = {
    "turkiye": "turkey",
    "usa": "usa",
    "congo dr": "congo dr",
    "south korea": "korea republic",
    "iran": "iran",
    "ivory coast": "cote d'ivoire",
    "czechia": "czech republic",
    "curacao": "curacao",
    "cape verde": "cape verde islands",
}


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("ü", "u").replace("ô", "o")


# Canonical team identity to reconcile Kalshi names with SportMonks/schedule names
# (Kalshi: "Czechia", "Turkiye"; ours: "Czech Republic", "Türkiye"; etc.)
_CANON = {
    "czech republic": "czechia", "czechia": "czechia",
    "turkey": "turkey", "turkiye": "turkey", "türkiye": "turkey",
    "south korea": "korea", "korea republic": "korea", "korea": "korea",
    "ivory coast": "ivory coast", "cote d'ivoire": "ivory coast",
    "côte d'ivoire": "ivory coast",
    "usa": "usa", "united states": "usa", "united states of america": "usa",
    "cape verde": "cape verde", "cape verde islands": "cape verde",
    "dr congo": "congo", "congo dr": "congo",
    "bosnia and herzegovina": "bosnia", "bosnia & herzegovina": "bosnia",
    "iran": "iran", "ir iran": "iran",
}


def canon_team(name: str) -> str:
    s = _norm(name)
    return _CANON.get(s, s)


def same_team(a: str, b: str) -> bool:
    ca, cb = canon_team(a), canon_team(b)
    return ca == cb or (len(ca) > 3 and ca in cb) or (len(cb) > 3 and cb in ca)


def team_in_text(text: str, name: str) -> bool:
    """Does a Kalshi string (title/sub) refer to team `name`?"""
    t = _norm(text)
    if _norm(name) and _norm(name) in t:
        return True
    c = canon_team(name)
    if c in t:
        return True
    return any(v in t for v, cc in _CANON.items() if cc == c)


def build_name_index(sm: SportMonks) -> dict:
    """name(lower) -> team_id for every team in the WC season."""
    idx = {}
    for t in sm.wc_teams():
        idx[_norm(t.get("name"))] = t.get("id")
    return idx


def resolve(sm: SportMonks, kalshi_name: str, index: dict | None = None) -> int | None:
    index = index if index is not None else build_name_index(sm)
    n = _norm(kalshi_name)
    if n in index:                          # direct match first (e.g. "türkiye")
        return index[n]
    a = _ALIASES.get(n)
    if a and a in index:                    # legacy alias map
        return index[a]
    for name, tid in index.items():         # canonical equivalence (handles all variants)
        if same_team(kalshi_name, name):
            return tid
    return None
