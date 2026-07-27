"""Convert the model's reasoned expected goals into derived-market probabilities.

The reasoning model supplies expected goals per team (xg_home, xg_away) — its
judgment, not a formula. This module does the *mechanical* step of turning two
expected-goal numbers into a scoreline distribution (Dixon-Coles adjusted
Poisson), from which we price totals, spreads, BTTS, team-totals, clean sheets.
One reasoning pass therefore prices many Kalshi markets.
"""
from math import exp, factorial

MAX_GOALS = 10
RHO = -0.10  # Dixon-Coles low-score dependency (typical empirical value)


def _pois(k: int, lam: float) -> float:
    return exp(-lam) * lam ** k / factorial(k)


def _dc_tau(x: int, y: int, lh: float, la: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1 - lh * la * rho
    if x == 0 and y == 1:
        return 1 + lh * rho
    if x == 1 and y == 0:
        return 1 + la * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def score_matrix(xg_home: float, xg_away: float,
                 max_goals: int = MAX_GOALS, rho: float = RHO) -> list[list[float]]:
    """Joint P(home=i, away=j) scoreline matrix, normalized."""
    xg_home = max(0.05, xg_home)
    xg_away = max(0.05, xg_away)
    m = [[_pois(i, xg_home) * _pois(j, xg_away) * _dc_tau(i, j, xg_home, xg_away, rho)
          for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    total = sum(sum(row) for row in m)
    return [[v / total for v in row] for row in m]


def derive_markets(xg_home: float, xg_away: float) -> dict:
    """Return probabilities for the derivative markets Kalshi lists per match."""
    m = score_matrix(xg_home, xg_away)
    n = len(m)
    p_home = sum(m[i][j] for i in range(n) for j in range(n) if i > j)
    p_draw = sum(m[i][i] for i in range(n))
    p_away = sum(m[i][j] for i in range(n) for j in range(n) if i < j)

    def total_over(line: float) -> float:
        return sum(m[i][j] for i in range(n) for j in range(n) if i + j > line)

    btts = sum(m[i][j] for i in range(1, n) for j in range(1, n))
    home_cs = sum(m[i][0] for i in range(n))   # away fails to score
    away_cs = sum(m[0][j] for j in range(n))    # home fails to score

    def team_over(side: str, line: float) -> float:
        if side == "home":
            return sum(m[i][j] for i in range(n) for j in range(n) if i > line)
        return sum(m[i][j] for i in range(n) for j in range(n) if j > line)

    def spread_home(handicap: float) -> float:
        # P(home margin + handicap > 0), i.e. home covers the handicap
        return sum(m[i][j] for i in range(n) for j in range(n) if (i - j) + handicap > 0)

    return {
        "p_home": round(p_home, 4),
        "p_draw": round(p_draw, 4),
        "p_away": round(p_away, 4),
        "over_0_5": round(total_over(0.5), 4),
        "over_1_5": round(total_over(1.5), 4),
        "over_2_5": round(total_over(2.5), 4),
        "over_3_5": round(total_over(3.5), 4),
        "btts_yes": round(btts, 4),
        "home_clean_sheet": round(home_cs, 4),
        "away_clean_sheet": round(away_cs, 4),
        "home_over_0_5": round(team_over("home", 0.5), 4),
        "home_over_1_5": round(team_over("home", 1.5), 4),
        "home_over_2_5": round(team_over("home", 2.5), 4),
        "home_over_3_5": round(team_over("home", 3.5), 4),
        "away_over_0_5": round(team_over("away", 0.5), 4),
        "away_over_1_5": round(team_over("away", 1.5), 4),
        "away_over_2_5": round(team_over("away", 2.5), 4),
        "away_over_3_5": round(team_over("away", 3.5), 4),
        "home_cover_-0.5": round(spread_home(-0.5), 4),
        "home_cover_-1.5": round(spread_home(-1.5), 4),
        "home_cover_-2.5": round(spread_home(-2.5), 4),
        "away_cover_-1.5": round(1 - spread_home(1.5), 4),
        "away_cover_-2.5": round(1 - spread_home(2.5), 4),
        "away_cover_+1.5": round(1 - spread_home(1.5), 4),
    }
