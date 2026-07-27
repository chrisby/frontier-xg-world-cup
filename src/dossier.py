"""Dossier assembler: gathers FACTS for the reasoning layer.

Deterministic data collection only. It computes no win probabilities and makes
no betting decision -- it produces a clean, structured, human+LLM readable
brief that the reasoning agent reasons over.
"""
from datetime import date, datetime
from statistics import mean
from typing import Optional

from .sportmonks import SportMonks

# SportMonks statistic type names we care about (shot/possession quality)
_STAT_KEYS = {
    "Goals": "goals",
    "Shots Total": "shots",
    "Shots On Target": "sot",
    "Ball Possession %": "possession",
    "Dangerous Attacks": "dangerous_attacks",
    "Corners": "corners",
}


def _age(dob: Optional[str]) -> Optional[int]:
    if not dob:
        return None
    try:
        d = datetime.strptime(dob[:10], "%Y-%m-%d").date()
        t = date.today()
        return t.year - d.year - ((t.month, t.day) < (d.month, d.day))
    except ValueError:
        return None


def _team_stats_in_fixture(fixture: dict, team_id: int) -> dict:
    """Pull this team's stat line + result from a fixture object."""
    out = {}
    for s in fixture.get("statistics", []) or []:
        if s.get("participant_id") != team_id:
            continue
        name = (s.get("type") or {}).get("name")
        if name in _STAT_KEYS:
            val = (s.get("data") or {}).get("value")
            out[_STAT_KEYS[name]] = val
    # result from scores (CURRENT/normal-time final)
    gf = ga = None
    for sc in fixture.get("scores", []) or []:
        if sc.get("description") not in ("CURRENT", "2ND_HALF", None):
            continue
        g = (sc.get("score") or {}).get("goals")
        if g is None:
            continue
        if sc.get("participant_id") == team_id or (sc.get("score") or {}).get(
            "participant"
        ) == "home" and _is_home(fixture, team_id):
            gf = g
        else:
            ga = g
    out["gf"], out["ga"] = gf, ga
    parts = fixture.get("participants", [])
    out["opponent"] = next(
        (p.get("name") for p in parts if p.get("id") != team_id), "?"
    )
    out["date"] = (fixture.get("starting_at") or "")[:10]
    out["competition"] = (fixture.get("league") or {}).get("name") or "?"
    comp = (fixture.get("league") or {}).get("name") or ""
    if "World Cup" in comp:
        out["venue"] = "N"
    else:
        out["venue"] = "H" if _is_home(fixture, team_id) else "A"
    return out


def _fmt_result(f: dict) -> str:
    """One recent-result line: score plus per-match shot-quality when present."""
    base = (f"{f['date']} ({f['venue']}, {f['competition']}) vs {f['opponent']}: "
            f"{f['gf']}-{f['ga']}")
    if f.get("shots") is not None or f.get("possession") is not None:
        bits = []
        if f.get("shots") is not None:
            bits.append(f"{f['shots']} sh")
        if f.get("sot") is not None:
            bits.append(f"{f['sot']} on target")
        if f.get("possession") is not None:
            bits.append(f"{f['possession']}% poss")
        if f.get("dangerous_attacks") is not None:
            bits.append(f"{f['dangerous_attacks']} dang.att")
        base += " [" + ", ".join(bits) + "]"
    return base


def _is_home(fixture: dict, team_id: int) -> bool:
    for p in fixture.get("participants", []):
        if p.get("id") == team_id:
            meta = p.get("meta") or {}
            return meta.get("location") == "home"
    return False


def build_team_dossier(sm: SportMonks, team_id: int, n_recent: int = 12) -> dict:
    team = sm.team(team_id)
    squad = sm.squad(team_id)
    recent = sm.recent_fixtures(team_id, n_recent)
    coach = sm.active_coach(team_id)
    sidelined = sm.sidelined(team_id)

    players = []
    for s in squad:
        p = s.get("player") or {}
        pid = p.get("id")
        st = (sm.player_recent_stats(pid).get("stats") or {}) if pid else {}
        players.append({
            "name": p.get("display_name"),
            "age": _age(p.get("date_of_birth")),
            "position": (p.get("position") or {}).get("name"),
            "goals": st.get("goals"),
            "assists": st.get("assists"),
            "appearances": st.get("appearances"),
            "minutes": st.get("minutes"),
        })

    form = [_team_stats_in_fixture(f, team_id) for f in recent]
    form = [f for f in form if f.get("gf") is not None]
    # Merge in friendlies (not covered by the SportMonks plan), newest first.
    try:
        from .friendlies import recent_friendlies
        form += recent_friendlies(team.get("name"), n=8)
    except Exception:
        pass  # friendlies are a bonus; never block the dossier on them
    form.sort(key=lambda f: f.get("date") or "", reverse=True)
    form = form[:n_recent]

    def avg(key):
        vals = [f[key] for f in form if f.get(key) is not None]
        return round(mean(vals), 2) if vals else None

    wins = sum(1 for f in form if (f["gf"] or 0) > (f["ga"] or 0))
    draws = sum(1 for f in form if (f["gf"] or 0) == (f["ga"] or 0))
    losses = len(form) - wins - draws

    injuries = []
    for s in sidelined:
        p = s.get("player") or {}
        if p.get("display_name"):
            injuries.append(p.get("display_name"))

    wc_form = [f for f in form if f.get("competition") == "World Cup"]
    pre_form = [f for f in form if f.get("competition") != "World Cup"]

    def _stats(subset):
        def s_avg(key):
            vals = [f[key] for f in subset if f.get(key) is not None]
            return round(mean(vals), 2) if vals else None
        w = sum(1 for f in subset if (f["gf"] or 0) > (f["ga"] or 0))
        d = sum(1 for f in subset if (f["gf"] or 0) == (f["ga"] or 0))
        l = len(subset) - w - d
        return {
            "matches": len(subset),
            "record": f"{w}W-{d}D-{l}L",
            "avg_goals_for": s_avg("gf"),
            "avg_goals_against": s_avg("ga"),
            "avg_shots": s_avg("shots"),
            "avg_shots_on_target": s_avg("sot"),
            "avg_possession": s_avg("possession"),
        }

    wc_stats = _stats(wc_form)
    pre_stats = _stats(pre_form)

    return {
        "team_id": team_id,
        "name": team.get("name"),
        "coach": coach.get("name") if coach else None,
        "coach_since": coach.get("since") if coach else None,
        "squad_size": len(players),
        "avg_age": round(mean([p["age"] for p in players if p["age"]]), 1)
        if any(p["age"] for p in players) else None,
        "players": players,
        "injuries": injuries,
        "wc_2026": {
            **wc_stats,
            "results": [_fmt_result(f) for f in wc_form],
        } if wc_form else None,
        "pre_tournament": {
            **pre_stats,
            "results": [_fmt_result(f) for f in pre_form[:6]],
        } if pre_form else None,
        # keep full form for backwards compat with publish/site code
        "form": {
            "matches": len(form),
            "record": f"{wins}W-{draws}D-{losses}L",
            "avg_goals_for": avg("gf"),
            "avg_goals_against": avg("ga"),
            "avg_shots": avg("shots"),
            "avg_shots_on_target": avg("sot"),
            "avg_possession": avg("possession"),
            "recent_results": [_fmt_result(f) for f in form[:8]],
        },
    }


def build_match_dossier(sm: SportMonks, home_id: int, away_id: int,
                        context: dict = None) -> dict:
    home = build_team_dossier(sm, home_id)
    away = build_team_dossier(sm, away_id)
    # confirmed starting XI (empty until ~1h before kickoff)
    fx = sm.find_match_fixture(home_id, away_id)
    lus = sm.lineup(fx["id"]) if fx else {}
    home["lineup"] = lus.get(home_id, [])
    away["lineup"] = lus.get(away_id, [])
    return {
        "context": context or {},
        "home": home,
        "away": away,
    }


def _format_xi(xi: list) -> str:
    """Group the confirmed XI by position into a concise block."""
    groups: dict = {}
    order = []
    for p in xi:
        detail, pos = p.get("detail"), p.get("position")
        if detail == "Attacking Midfield":
            cat = "Attacking Midfielders"
        elif pos == "Goalkeeper":
            cat = "Goalkeeper"
        elif pos == "Defender":
            cat = "Defenders"
        elif pos == "Midfielder":
            cat = "Midfielders"
        elif pos == "Attacker":
            cat = "Attackers"
        else:
            cat = "Other"
        if cat not in groups:
            groups[cat] = []
            order.append(cat)
        groups[cat].append(p["name"])
    cat_order = ["Goalkeeper", "Defenders", "Midfielders", "Attacking Midfielders",
                  "Attackers", "Other"]
    order = [c for c in cat_order if c in groups]
    return "**CONFIRMED STARTING XI**\n" + "\n".join(
        f"- {cat}: {', '.join(groups[cat])}" for cat in order)


def render_markdown(dossier: dict) -> str:
    """Human/LLM-readable brief for the reasoning agent."""
    def team_block(t):
        f = t["form"]
        coach = t["coach"] or "unknown"
        if t.get("coach_since"):
            coach += f" (since {t['coach_since']})"
        def pstat(p):
            bits = []
            if p.get("goals") is not None:
                bits.append(f"{p['goals']}G")
            if p.get("assists") is not None:
                bits.append(f"{p['assists']}A")
            if p.get("appearances") is not None:
                bits.append(f"{p['appearances']}app")
            s = ("/".join(bits)) if bits else "no recent caps"
            return f"{p['name']} ({p['position'] or '?'}, {p['age']}; {s})"

        squad_lines = "\n".join(f"  - {pstat(p)}" for p in t["players"] if p["name"])
        # key recent goal contributors (national-team season)
        scorers = sorted(
            [p for p in t["players"] if (p.get("goals") or 0) or (p.get("assists") or 0)],
            key=lambda p: ((p.get("goals") or 0) + (p.get("assists") or 0)), reverse=True,
        )[:5]
        contrib = ", ".join(
            f"{p['name']} ({p.get('goals') or 0}G {p.get('assists') or 0}A)" for p in scorers
        ) or "none recorded this cycle"
        inj = ", ".join(t.get("injuries", [])) or "none reported"

        def fmt(v, suffix=""):
            return f"{v}{suffix}" if v is not None else "n/a"
        xi = t.get("lineup") or []
        xi_line = _format_xi(xi) if xi else "Starting XI not yet announced (squad below)"
        past_block = ""

        wc = t.get("wc_2026")
        pre = t.get("pre_tournament")
        wc_block = ""
        if wc and wc["matches"] > 0:
            wc_block = (
                f"- **WC 2026 so far ({wc['matches']} game{'s' if wc['matches']>1 else ''}): "
                f"{wc['record']} | "
                f"Goals {fmt(wc['avg_goals_for'])}/{fmt(wc['avg_goals_against'])} | "
                f"Shots {fmt(wc['avg_shots'])} ({fmt(wc['avg_shots_on_target'])} on target) | "
                f"Poss {fmt(wc['avg_possession'], '%')}**\n"
                f"  " + "\n  ".join(wc["results"]) + "\n"
            )
        pre_block = ""
        if pre and pre["matches"] > 0:
            pre_block = (
                f"- Pre-tournament form ({pre['matches']} games): {pre['record']} | "
                f"Goals {fmt(pre['avg_goals_for'])}/{fmt(pre['avg_goals_against'])} | "
                f"Shots {fmt(pre['avg_shots'])} ({fmt(pre['avg_shots_on_target'])} on target) | "
                f"Poss {fmt(pre['avg_possession'], '%')}\n"
                f"  " + "\n  ".join(pre["results"]) + "\n"
            )
        return (
            f"### {t['name']}\n"
            f"{xi_line}\n"
            f"- Coach: {coach} | Squad avg age: {t['avg_age']}\n"
            + past_block + wc_block + pre_block +
            f"- Key goal contributors (recent cycle): {contrib}\n"
            f"- Reported injuries/sidelined: {inj}\n"
            f"- Squad (player: goals/assists/apps this cycle):\n{squad_lines}\n"
        )

    if "home" in dossier:
        ctx = dossier.get("context", {})
        return (
            f"## Match dossier\nContext: {ctx}\n\n"
            + team_block(dossier["home"]) + "\n" + team_block(dossier["away"])
        )
    return team_block(dossier)
