"""Supplement the dossier with international friendlies (free dataset).

The SportMonks "World Cup 2026" plan covers only qualifiers + the tournament, so
warm-up friendlies are invisible to it. martj42/international_results is a free,
regularly-updated CSV of every international match since 1872, including
friendlies. We pull recent friendlies per nation and merge them into recent form.

Friendlies carry score + opponent + venue but no shot-quality stats.
"""
import csv
import datetime
import time
from pathlib import Path

import requests

from . import config

_CSV_URL = ("https://raw.githubusercontent.com/martj42/"
            "international_results/master/results.csv")
_CACHE = config.DATA_DIR / "intl_results.csv"
_MAX_AGE = 24 * 3600

# SportMonks team name -> dataset team name, where they differ
_NAME_MAP = {
    "usa": "United States",
    "korea republic": "South Korea",
    "cote d'ivoire": "Ivory Coast",
    "china pr": "China",
    "cape verde islands": "Cape Verde",
    "czechia": "Czech Republic",
    "congo dr": "DR Congo",
    "turkiye": "Turkey",
    "iran": "Iran",
}


def _ensure_csv() -> Path:
    if _CACHE.exists() and time.time() - _CACHE.stat().st_mtime < _MAX_AGE:
        return _CACHE
    r = requests.get(_CSV_URL, timeout=60)
    r.raise_for_status()
    _CACHE.write_bytes(r.content)
    return _CACHE


_ROWS = None


def _load() -> list:
    global _ROWS
    if _ROWS is None:
        with open(_ensure_csv(), newline="") as f:
            _ROWS = list(csv.DictReader(f))
    return _ROWS


def _csv_name(sm_name: str) -> str:
    return _NAME_MAP.get((sm_name or "").strip().lower(), sm_name)


def recent_friendlies(sm_name: str, n: int = 8,
                      lookback_days: int = 900) -> list:
    """Recent friendly results for a nation, newest first, dossier-shaped."""
    name = _csv_name(sm_name)
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=lookback_days)).isoformat()
    today = datetime.date.today().isoformat()
    out = []
    for r in _load():
        if r["tournament"] != "Friendly":
            continue
        if r["date"] < cutoff or r["date"] > today:
            continue
        home, away = r["home_team"], r["away_team"]
        if name not in (home, away):
            continue
        try:
            hs, as_ = int(r["home_score"]), int(r["away_score"])
        except (ValueError, KeyError):
            continue
        is_home = name == home
        neutral = (r.get("neutral", "").lower() == "true")
        out.append({
            "date": r["date"],
            "opponent": away if is_home else home,
            "gf": hs if is_home else as_,
            "ga": as_ if is_home else hs,
            "competition": "Friendly",
            "venue": "N" if neutral else ("H" if is_home else "A"),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:n]
