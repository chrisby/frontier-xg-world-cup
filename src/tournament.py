"""Monte-Carlo the 2026 World Cup to price futures markets.

Group stage is modeled EXACTLY (real groups, real 48-team format: top 2 of each
group + 8 best third-placed advance to the Round of 32). The knockout bracket is
a seeded single-elimination over the 32 qualifiers — an approximation of FIFA's
fixed slotting, so group-winner and advancement probabilities are exact, while
reach-R16/QF/SF/final are well-calibrated but not bracket-exact.

If per-team player goal-shares are supplied, each simulated goal is allocated to
a scorer (multinomial draw), so we also get P(Golden Boot) per player.
"""
from collections import defaultdict

import numpy as np

from . import config
from .sportmonks import SportMonks
from .team_rating import LEAGUE_AVG

WC_SEASON = config.WC_SEASON_ID


def get_groups(sm: SportMonks) -> dict:
    d = sm.get(f"standings/seasons/{WC_SEASON}",
               {"include": "participant;group"}, cache_hours=72)
    groups = defaultdict(list)
    for r in d.get("data", []):
        g = (r.get("group") or {}).get("name")
        p = r.get("participant") or {}
        if g and p.get("name"):
            groups[g].append(p.get("name"))
    return dict(sorted(groups.items()))


def _lambdas(ra: dict, rb: dict) -> tuple:
    la = ra["off_rating"] * rb["def_rating"] / LEAGUE_AVG
    lb = rb["off_rating"] * ra["def_rating"] / LEAGUE_AVG
    return max(0.05, la), max(0.05, lb)


def _alloc(team: str, g: int, shares: dict, tally: dict, rng):
    """Distribute g goals among a team's players by goal-share (multinomial)."""
    if not shares or g <= 0:
        return
    sh = shares.get(team)
    if not sh:
        return
    players = list(sh)
    probs = np.array([sh[p] for p in players], dtype=float)
    if probs.sum() <= 0:
        return
    probs = probs / probs.sum()
    for p, c in zip(players, rng.multinomial(g, probs)):
        if c:
            tally[p] += int(c)


def _ko_winner(a, b, R, rng, shares=None, tally=None, results=None) -> str:
    if results and frozenset({a, b}) in results.get("ko", {}):
        return results["ko"][frozenset({a, b})]  # actual knockout result
    la, lb = _lambdas(R[a], R[b])
    ga, gb = int(rng.poisson(la)), int(rng.poisson(lb))
    _alloc(a, ga, shares, tally, rng)
    _alloc(b, gb, shares, tally, rng)
    if ga > gb:
        return a
    if gb > ga:
        return b
    sa = R[a]["off_rating"] / R[a]["def_rating"]
    sb = R[b]["off_rating"] / R[b]["def_rating"]
    return a if rng.random() < sa / (sa + sb) else b


def _sim_group(teams, R, rng, shares=None, tally=None, results=None):
    pts = {t: 0 for t in teams}
    gd = {t: 0 for t in teams}
    gf = {t: 0 for t in teams}
    played = (results or {}).get("group", {})
    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            a, b = teams[i], teams[j]
            actual = played.get(frozenset({a, b}))
            if actual:  # this group game already happened — use the real score
                ga, gb = actual[a], actual[b]
            else:
                la, lb = _lambdas(R[a], R[b])
                ga, gb = int(rng.poisson(la)), int(rng.poisson(lb))
            _alloc(a, ga, shares, tally, rng)
            _alloc(b, gb, shares, tally, rng)
            gf[a] += ga; gf[b] += gb
            gd[a] += ga - gb; gd[b] += gb - ga
            if ga > gb:
                pts[a] += 3
            elif gb > ga:
                pts[b] += 3
            else:
                pts[a] += 1; pts[b] += 1
    ranked = sorted(teams, key=lambda t: (pts[t], gd[t], gf[t], rng.random()),
                    reverse=True)
    return ranked, pts, gd, gf


def simulate_once(groups, R, rng, shares=None, results=None):
    reach = defaultdict(lambda: defaultdict(bool))
    tally = defaultdict(int) if shares else None
    winners, runners, thirds = [], [], []
    for teams in groups.values():
        ranked, pts, gd, gf = _sim_group(teams, R, rng, shares, tally, results)
        winners.append(ranked[0]); runners.append(ranked[1])
        thirds.append((ranked[2], pts[ranked[2]], gd[ranked[2]], gf[ranked[2]]))
        reach[ranked[0]]["win_group"] = True
        for t in ranked[:2]:
            reach[t]["advance"] = True

    thirds.sort(key=lambda x: (x[1], x[2], x[3], rng.random()), reverse=True)
    for t, *_ in thirds[:8]:
        reach[t]["advance"] = True

    seeds = winners + runners + [t[0] for t in thirds[:8]]   # 32
    teams_in = []
    for i in range(len(seeds) // 2):
        a, b = seeds[i], seeds[len(seeds) - 1 - i]
        reach[a]["reach_r32"] = reach[b]["reach_r32"] = True
        teams_in += [a, b]

    round_names = ["reach_r16", "reach_qf", "reach_sf", "reach_final", "champion"]
    rn = 0
    while len(teams_in) > 1:
        nxt = [_ko_winner(teams_in[i], teams_in[i + 1], R, rng, shares, tally, results)
               for i in range(0, len(teams_in), 2)]
        for t in nxt:
            reach[t][round_names[rn]] = True
        teams_in = nxt
        rn += 1

    golden = max(tally, key=tally.get) if tally else None
    return reach, golden


def monte_carlo(groups, R, n=20000, seed=0, shares=None, results=None) -> dict:
    rng = np.random.default_rng(seed)
    counts = defaultdict(lambda: defaultdict(int))
    boot = defaultdict(int)
    all_teams = [t for g in groups.values() for t in g]
    for _ in range(n):
        reach, golden = simulate_once(groups, R, rng, shares, results)
        for t in all_teams:
            for k, v in reach[t].items():
                if v:
                    counts[t][k] += 1
        if golden:
            boot[golden] += 1
    markets = ["win_group", "advance", "reach_r16", "reach_qf",
               "reach_sf", "reach_final", "champion"]
    return {
        "teams": {t: {m: round(counts[t][m] / n, 4) for m in markets}
                  for t in all_teams},
        "golden_boot": {p: round(c / n, 4)
                        for p, c in sorted(boot.items(), key=lambda x: -x[1])},
    }
