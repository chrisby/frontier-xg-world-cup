"""Player goalscorer props (e.g. anytime goalscorer).

Reuses the match reasoner's team expected goals. A second reasoning pass splits
that team total into per-player expected goals — the model judges role, penalty
duties, form and the per-player goal/assist stats already in the dossier (not a
fixed share formula). P(anytime scorer) = 1 - exp(-player_expected_goals).
"""
from math import exp

from . import llm

_SYSTEM = """You allocate a team's expected goals for a single 2026 World Cup \
match among its players. You are told the team's TOTAL expected goals; split it \
into expected goals per player.

Rules:
- Use role (strikers/attacking mids/wingers score most; defenders little), the \
per-player goals/assists/appearances given, who likely takes penalties and set \
pieces, minutes/fitness, and the opponent's defensive strength.
- Only list players realistically in the XI / likely to feature. Their expected \
goals should sum to roughly the team total you are given.
- Be calibrated: in a typical match even a lead striker's expected goals is \
~0.3-0.6; nobody is above ~0.9 in a single game."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "scorers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "exp_goals": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["name", "exp_goals", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scorers"],
    "additionalProperties": False,
}


_SHARES_SYSTEM = """You estimate how a nation's goals over the 2026 World Cup \
will be distributed among its players — for pricing the Golden Boot (top scorer).

Output each likely scorer's SHARE of the team's total tournament goals.

Rules:
- Weigh role (out-and-out strikers and wide forwards score most; the focal No.9 \
often takes a big share), who takes PENALTIES and direct free kicks (a penalty \
taker on a high-scoring side can dominate the Golden Boot), recent goal form and \
the per-player goals/assists given, and expected minutes/fitness.
- Don't be misled by small samples: a backup who scored in two friendlies is not \
the focal point. Identify the genuine first-choice scorers.
- Only list realistic contributors; shares should sum to roughly 1.0."""

_SHARES_SCHEMA = {
    "type": "object",
    "properties": {
        "shares": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "share": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["name", "share", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["shares"],
    "additionalProperties": False,
}


def reason_goal_shares(team_name: str, players: list) -> dict:
    """Reasoned {player: share of team's tournament goals}. Sums to ~1."""
    roster = "\n".join(
        f"- {p['name']} ({p.get('position') or '?'}): "
        f"{p.get('goals') or 0}G {p.get('assists') or 0}A in "
        f"{p.get('appearances') or 0} apps"
        for p in players if p.get("name")
    )
    user = (f"{team_name} squad (recent international form):\n{roster}\n\n"
            f"Give each likely scorer's share of {team_name}'s World Cup goals.")
    shares = llm.complete_json(_SHARES_SYSTEM, user, _SHARES_SCHEMA,
                               max_tokens=3000, effort="high")["shares"]
    s = sum(max(0.0, x["share"]) for x in shares)
    if s <= 0:
        return {}
    return {x["name"]: round(x["share"] / s, 4) for x in shares if x["share"] > 0}


def reason_scorers(team_name: str, players: list, team_xg: float, opponent: str) -> list:
    """Return [{name, exp_goals, p_anytime, note}] for the team's likely scorers."""
    roster = "\n".join(
        f"- {p['name']} ({p.get('position') or '?'}): "
        f"{p.get('goals') or 0}G {p.get('assists') or 0}A in {p.get('appearances') or 0} apps"
        for p in players if p.get("name")
    )
    user = (f"{team_name} play {opponent}. {team_name}'s total expected goals this "
            f"match is {team_xg:.2f}. Allocate it across these players:\n\n{roster}")
    scorers = llm.complete_json(_SYSTEM, user, _SCHEMA,
                                max_tokens=4000, effort="high")["scorers"]
    # normalize to the team total, then convert to anytime-scorer probability
    s = sum(max(0.0, x["exp_goals"]) for x in scorers)
    if s > 0:
        for x in scorers:
            x["exp_goals"] = round(x["exp_goals"] * team_xg / s, 3)
    for x in scorers:
        x["p_anytime"] = round(1 - exp(-x["exp_goals"]), 4)
    return sorted(scorers, key=lambda x: -x["p_anytime"])


def render_scorers(team_name: str, scorers: list) -> str:
    lines = [f"Anytime goalscorer — {team_name}:"]
    for x in scorers[:8]:
        lines.append(f"  {x['name']:<24} {x['p_anytime']:5.1%}  "
                     f"(xg {x['exp_goals']:.2f}) — {x['note']}")
    return "\n".join(lines)
