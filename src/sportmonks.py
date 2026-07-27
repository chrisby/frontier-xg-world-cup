"""SportMonks v3 client with on-disk caching (respects rate limits).

Read-only data gathering for the dossier layer. Never makes betting decisions.
"""
import datetime
import json
import sqlite3
import time
from typing import Any, Optional

import requests

from . import config


def _is_placeholder(name: str) -> bool:
    """A not-yet-resolved bracket slot, e.g. '2nd Group G' or 'Winner QF1'."""
    n = (name or "").strip()
    return (not n) or n[0].isdigit() or n.startswith("Winner") or n.startswith("Loser") or "Group" in n


class _Cache:
    """Tiny SQLite KV cache so we don't re-hit the API (3000/hr per entity)."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, ts REAL, v TEXT)"
        )
        self.db.commit()

    def get(self, key: str, max_age: float) -> Optional[Any]:
        row = self.db.execute("SELECT ts, v FROM cache WHERE k=?", (key,)).fetchone()
        if not row:
            return None
        ts, v = row
        if time.time() - ts > max_age:
            return None
        return json.loads(v)

    def put(self, key: str, value: Any):
        self.db.execute(
            "INSERT OR REPLACE INTO cache (k, ts, v) VALUES (?,?,?)",
            (key, time.time(), json.dumps(value)),
        )
        self.db.commit()


class SportMonks:
    def __init__(self, api_key: str = None):
        self.key = api_key or config.MONKS_API_KEY
        if not self.key:
            raise RuntimeError("MONKS_API_KEY missing")
        self.cache = _Cache(config.CACHE_DB)
        self.session = requests.Session()

    def get(self, path: str, params: dict = None, cache_hours: float = 3.0) -> dict:
        params = dict(params or {})
        params["api_token"] = self.key
        # cache key excludes the token
        ck = f"{path}?{json.dumps({k: v for k, v in params.items() if k != 'api_token'}, sort_keys=True)}"
        hit = self.cache.get(ck, cache_hours * 3600)
        if hit is not None:
            return hit
        url = f"{config.SPORTMONKS_BASE}/{path.lstrip('/')}"
        for attempt in range(4):
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            self.cache.put(ck, data)
            return data
        r.raise_for_status()
        return {}

    def paged(self, path: str, params: dict = None, cache_hours: float = 3.0,
              max_pages: int = 10) -> list:
        """Follow pagination, returning the concatenated `data` arrays."""
        out, page = [], 1
        params = dict(params or {})
        while page <= max_pages:
            params["page"] = page
            d = self.get(path, params, cache_hours)
            out.extend(d.get("data", []))
            pg = d.get("pagination") or {}
            if not pg.get("has_more"):
                break
            page += 1
        return out

    # --- convenience wrappers used by the dossier layer ---
    def team(self, team_id: int, includes: str = "") -> dict:
        p = {"include": includes} if includes else {}
        return self.get(f"teams/{team_id}", p).get("data", {})

    def squad(self, team_id: int) -> list:
        return self.get(
            f"squads/teams/{team_id}", {"include": "player.position"}
        ).get("data", [])

    def recent_fixtures(self, team_id: int, n: int = 12, lookback_days: int = 900) -> list:
        """Clean, dated, competition-labeled recent matches (newest first).

        Uses fixtures/between (not the `latest` include) to avoid date gaps and
        stale-season bleed, and to carry the competition name so the reasoning
        layer can weight opponent/competition quality.
        """
        end = datetime.date.today()
        start = end - datetime.timedelta(days=lookback_days)
        d = self.get(
            f"fixtures/between/{start}/{end}/{team_id}",
            {"include": "participants;scores;statistics.type;league;state",
             "per_page": 50},
            cache_hours=3.0,
        )
        fx = d.get("data", []) or []
        fx.sort(key=lambda f: f.get("starting_at") or "", reverse=True)
        return fx[:n]

    def active_coach(self, team_id: int) -> Optional[dict]:
        d = self.team(team_id, includes="coaches.coach")
        cs = d.get("coaches", []) or []
        act = [c for c in cs if c.get("active")]
        pick = act[0] if act else (
            sorted(cs, key=lambda x: x.get("start") or "", reverse=True)[0] if cs else None
        )
        if not pick:
            return None
        return {"name": (pick.get("coach") or {}).get("name"), "since": pick.get("start")}

    def sidelined(self, team_id: int) -> list:
        d = self.team(team_id, includes="sidelined.player")
        return d.get("sidelined", []) or []

    def schedule(self) -> list:
        """Full WC schedule: chronological fixtures with kickoff + stage."""
        d = self.get(f"schedules/seasons/{config.WC_SEASON_ID}", cache_hours=2)
        out = []
        for stage in d.get("data", []):
            for rnd in stage.get("rounds", []):
                for f in rnd.get("fixtures", []):
                    out.append({
                        "start": f.get("starting_at"),
                        "name": f.get("name"),
                        "stage": stage.get("name"),
                        "placeholder": bool(f.get("placeholder")),
                    })
        out.sort(key=lambda x: x["start"] or "")
        return out

    def player_recent_stats(self, player_id: int) -> dict:
        """Most-recent-season national-team stats for one player.

        Iterates per player (one call each, cached). Picks the statistics block
        with the highest season_id so we get current-cycle form, and pulls the
        attacking/defensive detail types useful for reasoning + goalscorer props.
        """
        d = self.get(
            f"players/{player_id}",
            {"include": "statistics.details.type;statistics.season;position"},
        ).get("data", {})
        blocks = d.get("statistics", []) or []
        if not blocks:
            return {"name": d.get("display_name"), "stats": None}

        wanted = {
            "Goals": "goals", "Assists": "assists", "Minutes Played": "minutes",
            "Appearances": "appearances", "Shots Total": "shots",
            "Shots On Target": "shots_on_target", "Rating": "rating",
            "Goals Conceded": "goals_conceded", "Saves": "saves",
        }

        # Merge all detail blocks per season (SportMonks splits them).
        by_season: dict = {}
        for b in blocks:
            sid = b.get("season_id") or 0
            agg = by_season.setdefault(sid, {})
            for det in b.get("details", []) or []:
                name = (det.get("type") or {}).get("name")
                if name in wanted:
                    val = det.get("value") or {}
                    v = val.get("total", val.get("average"))
                    if v is not None:
                        agg[wanted[name]] = v

        # Pick the most recent season that actually has data (the WC season
        # itself is empty until games are played).
        seasons = [s for s in sorted(by_season, reverse=True)
                   if by_season[s].get("appearances") or by_season[s].get("minutes")]
        sid = seasons[0] if seasons else (max(by_season) if by_season else None)
        return {
            "name": d.get("display_name"),
            "position": (d.get("position") or {}).get("name"),
            "season_id": sid,
            "stats": by_season.get(sid, {}) if sid is not None else {},
        }

    def wc_results(self) -> dict:
        """Finished WC games for conditioning the futures sim on reality.

        Returns {"group": {frozenset(names): {name: goals}},
                 "ko":    {frozenset(names): winner_name}}.
        Empty until games are played, so it's a no-op pre-tournament.
        """
        fx = self.paged(
            "fixtures",
            {"filters": f"fixtureSeasons:{config.WC_SEASON_ID}",
             "include": "participants;state;scores;group", "per_page": 50},
            cache_hours=0.2)
        finished = {"FT", "AET", "FT_PEN"}
        group_res, ko_res = {}, {}
        for f in fx:
            if (f.get("state") or {}).get("developer_name") not in finished:
                continue
            parts = f.get("participants", [])
            if len(parts) != 2:
                continue
            names = {p["id"]: p["name"] for p in parts}
            if any(_is_placeholder(n) for n in names.values()):
                continue
            goals = {s.get("participant_id"): (s.get("score") or {}).get("goals")
                     for s in f.get("scores", [])
                     if s.get("description") == "CURRENT"}
            ids = list(names)
            if len(goals) != 2 or any(goals.get(i) is None for i in ids):
                continue
            a, b = ids
            key = frozenset({names[a], names[b]})
            if (f.get("group") or {}).get("name"):
                group_res[key] = {names[a]: goals[a], names[b]: goals[b]}
            elif goals[a] != goals[b]:  # knockout winner (skip pen draws for v1)
                ko_res[key] = names[a] if goals[a] > goals[b] else names[b]
        return {"group": group_res, "ko": ko_res}

    def find_match_fixture(self, home_id: int, away_id: int) -> Optional[dict]:
        """The WC fixture between two teams (for lineups), or None."""
        for f in self.wc_fixtures():
            ids = {p.get("id") for p in f.get("participants", [])}
            if home_id in ids and away_id in ids:
                return f
        return None

    def lineup(self, fixture_id: int) -> dict:
        """Confirmed starting XI per team:
        {team_id: [{"name","position","detail","slot"}]}, ordered by formation slot.

        Empty until SportMonks publishes lineups (~1h before kickoff). Cached
        briefly so re-analysis near kickoff picks up the latest XI.
        """
        d = self.get(f"fixtures/{fixture_id}",
                     {"include": "lineups.player;lineups.type;lineups.position;"
                                  "lineups.detailedposition"}, cache_hours=0.1)
        out: dict = {}
        for ln in (d.get("data", {}) or {}).get("lineups", []) or []:
            if (ln.get("type") or {}).get("name") != "Lineup":
                continue
            nm = (ln.get("player") or {}).get("display_name")
            if not nm:
                continue
            out.setdefault(ln.get("team_id"), []).append({
                "name": nm,
                "position": (ln.get("position") or {}).get("name"),
                "detail": (ln.get("detailedposition") or {}).get("name"),
                "slot": ln.get("formation_position"),
            })
        for players in out.values():
            players.sort(key=lambda p: p["slot"] or 99)
        return out

    def head_to_head(self, id1: int, id2: int) -> list:
        d = self.get(
            f"fixtures/head-to-head/{id1}/{id2}",
            {"include": "participants;scores;league"},
        )
        fx = d.get("data", []) or []
        fx.sort(key=lambda f: f.get("starting_at") or "", reverse=True)
        return fx

    def wc_fixtures(self) -> list:
        return self.paged(
            "fixtures",
            {"filters": f"fixtureSeasons:{config.WC_SEASON_ID}",
             "include": "participants;state;round;stage;group;scores", "per_page": 50},
        )

    def wc_teams(self) -> list:
        return self.get(
            f"teams/seasons/{config.WC_SEASON_ID}", {"per_page": 50}
        ).get("data", [])
