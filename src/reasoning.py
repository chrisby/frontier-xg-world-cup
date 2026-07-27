"""Reasoning layer: an LLM reasons through a dossier to a calibrated probability.

This is deliberately NOT a fixed formula. The model weighs squad quality, coach,
form, injuries, context, and history the way an analyst would, and returns a
probability plus its written reasoning. The betting market is never shown to it,
so its estimate is independent and can disagree with the market (= our edge).
"""
from . import llm

_SYSTEM = """You are a world-class football (soccer) analyst building an \
independent, bottom-up probability estimate for a single 2026 World Cup match.

Hard rules:
- Reason ONLY from the supplied dossier and your football knowledge. You are \
NOT given betting odds, and you must NOT try to recall or guess any market \
price. Your job is an INDEPENDENT estimate; anchoring to a market would defeat \
the purpose.
- Weigh ALL of: squad quality and depth (the named players, their clubs and \
level), the coach and how long they have been in charge (a recent appointment \
means less settled), recent form INCLUDING shot and possession quality rather \
than just W/D/L, injuries/suspensions versus the likely XI, head-to-head, \
fatigue/travel, and World Cup context (host advantage for USA/Canada/Mexico, \
heat, altitude, knockout vs group stakes).
- If a CONFIRMED STARTING XI is given, treat it as the most important and \
freshest input: check who is missing versus the team's usual best XI (a rested \
or injured star striker / key defender materially shifts the probabilities) and \
weight it heavily. If the XI is "not yet announced", reason from the squad and \
note that lineups could still move your estimate.
- CRITICAL — weight opponent and competition quality. A run of big wins in weak \
qualifying (e.g. vs Myanmar, Bahrain) is far less predictive than results vs \
strong sides. Each result line gives venue (H/A) and competition — use them to \
discount inflated goal/shot numbers earned against weak opposition.
- Calibrate carefully to the match context: is this a group stage game or a \
knockout? In knockouts, regulation-time dynamics differ — teams may play more \
cautiously knowing a draw leads to extra time rather than elimination.
- Output probabilities for home win / draw / away win that sum to ~1.0.
- ALSO output the expected goals you think each team will score in this match \
(xg_home, xg_away). These feed totals, spreads, and team-goal markets — reason \
about them carefully and independently of your win probabilities. Start from \
opponent defensive quality: a weak defence concedes space, tires, and can \
capitulate; a strong defence suppresses even good attacks. Do not anchor to \
tournament-wide averages, which blend mismatches and tight games equally. Weight \
this tournament's actual results heavily — how many goals each team has scored \
and conceded in their WC 2026 games is the freshest signal; pre-tournament \
friendlies and qualifiers are useful context but should yield to what teams have \
actually shown in this competition. Before finalising xg, ask yourself: does this \
estimate reflect the opponent's actual defensive quality, or have I drifted toward \
the tournament average?

Think carefully and self-critically about what the data does and does not show, \
then give your calibrated estimate and the reasoning behind it."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "p_home": {"type": "number"},
        "p_draw": {"type": "number"},
        "p_away": {"type": "number"},
        "xg_home": {"type": "number"},
        "xg_away": {"type": "number"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "key_factors": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["p_home", "p_draw", "p_away", "xg_home", "xg_away",
                 "confidence", "key_factors", "rationale"],
    "additionalProperties": False,
}


def reason_match(dossier_markdown: str, home: str, away: str,
                 phase: str = "group") -> dict:
    """Return calibrated 3-way probabilities + rationale for home vs away."""
    system = _SYSTEM
    if phase == "knockout":
        phase_note = (
            "This is a KNOCKOUT match. You are estimating REGULATION TIME (90-min) "
            "probabilities only — a draw means the game proceeds to extra time and "
            "penalties, not elimination."
        )
    else:
        phase_note = "This is a GROUP STAGE match."
    user = (
        f"Match: {home} vs {away} at the 2026 World Cup (neutral venue — ignore any "
        f"home/away designation, there is no home-field advantage). {phase_note}\n\n"
        f"{dossier_markdown}\n\n"
        f"Give your independent calibrated probabilities for {home} win, draw, "
        f"and {away} win, the single most important factors, and your reasoning."
    )
    out = llm.complete_json(system, user, _SCHEMA, max_tokens=16000, effort="high")
    # Normalise the three probabilities defensively (schema can't enforce sum=1).
    s = out["p_home"] + out["p_draw"] + out["p_away"]
    if s > 0:
        for k in ("p_home", "p_draw", "p_away"):
            out[k] = round(out[k] / s, 4)
    out["model"] = llm.model_label()
    out["prompt_snapshot"] = {"system": system, "user": user}
    return out
