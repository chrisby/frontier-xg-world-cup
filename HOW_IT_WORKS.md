# How the system works — exact behavior

This document describes precisely what the system does, end to end, including the
exact prompt sent to the reasoning model.

## Philosophy
1. **Code gathers facts. The model forms the judgment.** No hardcoded rating
   formula — an LLM *reasons* over the evidence the way an analyst would.
2. **Independent of the market.** The reasoning model is never shown the betting
   price, so its probability is its own. The market is consulted only at the end,
   to measure disagreement (= our edge).
3. **Human-approved.** Every bet is presented on a sheet; you confirm before any
   order is placed.

## Step by step

### 1. Find the market (Kalshi, public read API)
`KXWCGAME` = per-match 3-way markets (`TEAM1` / `TIE` / `TEAM2`), each a binary
contract priced in cents (cents = implied probability). We also have access to
`KXMENWORLDCUP` (outright winner), group, reach-round, and prop markets.

### 2. Assemble the dossier (SportMonks → `src/dossier.py`)
For each team we gather, deterministically:
- **Squad** — up to 15 named players with position and age.
- **Coach** — name + appointment date (so the model knows how settled the side is).
- **Recent form** — last ~12 competitive matches via `fixtures/between`, newest
  first, each line tagged with **venue (H/A)** and **competition**, plus the score.
- **Shot-quality aggregates** — goals for/against, shots, shots on target,
  possession % per game (not just W/D/L).
- **Injuries** — reported sidelined players.
- **Head-to-head** — recent meetings between the two sides.

> ⚠️ **Data coverage caveat:** the current SportMonks plan includes only the WC
> qualifier leagues and the tournament. **International friendlies are not
> covered**, so warm-up friendlies do not yet feed the model. See "Data coverage".

### 3. Reason to a probability (`src/reasoning.py`, model `claude-fable-5`)
The dossier is rendered to markdown and sent to Fable 5 with **adaptive thinking**
and **high effort**, constrained to a JSON schema. The model returns calibrated
home/draw/away probabilities, the key factors, and its written rationale.

#### Exact system prompt
```
You are a world-class football (soccer) analyst building an independent,
bottom-up probability estimate for a single 2026 World Cup match.

Hard rules:
- Reason ONLY from the supplied dossier and your football knowledge. You are
NOT given betting odds, and you must NOT try to recall or guess any market
price. Your job is an INDEPENDENT estimate; anchoring to a market would defeat
the purpose.
- Weigh ALL of: squad quality and depth (the named players, their clubs and
level), the coach and how long they have been in charge (a recent appointment
means less settled), recent form INCLUDING shot and possession quality rather
than just W/D/L, injuries/suspensions versus the likely XI, head-to-head,
fatigue/travel, and World Cup context (host advantage for USA/Canada/Mexico,
heat, altitude, knockout vs group stakes).
- CRITICAL — weight opponent and competition quality. A run of big wins in weak
qualifying (e.g. vs Myanmar, Bahrain) is far less predictive than results vs
strong sides. Each result line gives venue (H/A) and competition - use them to
discount inflated goal/shot numbers earned against weak opposition.
- Calibrate to international football base rates: draws are common (~24% of
matches), and even strong favourites rarely exceed ~70% to win a single game.
- Output probabilities for home win / draw / away win that sum to ~1.0.

Think carefully and self-critically about what the data does and does not show,
then give your calibrated estimate and the reasoning behind it.
```

#### Exact user message (template)
```
Match: {HOME} (home) vs {AWAY} (away) at the 2026 World Cup.

{DOSSIER_MARKDOWN}

Give your independent calibrated probabilities for {HOME} win, draw, and {AWAY}
win, the single most important factors, and your reasoning.
```

#### Output schema (enforced)
```json
{
  "p_home": number, "p_draw": number, "p_away": number,
  "confidence": "low" | "medium" | "high",
  "key_factors": [string, ...],
  "rationale": string
}
```
The three probabilities are normalized to sum to 1.0 after the call.

### 4. Find edge + size the bet (`src/edge.py`)
For each outcome we compare the model probability `p` to Kalshi's `yes_ask`
price `c` (in dollars):
- **Edge** = `p − c`. We require **edge ≥ 4%** after fees.
- **Stake** = half-Kelly: `0.5 × (p − c) / (1 − c) × bankroll`, capped at **$30**
  per bet on a **$250** bankroll.
- Kalshi's trading fee (`≈ 0.07 × contracts × c × (1−c)`) must be smaller than
  the edge, or the bet is dropped.

The result is a **bet sheet** you confirm before anything is placed.

## Worked example (Japan vs Sweden, real output)
The model made Japan **41% / draw 27% / Sweden 32%**, reasoning that Japan's
9W-2D-1L qualifying record was inflated by weak opposition (Indonesia, Bahrain),
that the informative data points were the 0-1 loss in Australia and 0-0 vs Saudi
Arabia, that Japan are settled under Moriyasu (since 2018) while Sweden's Potter
arrived in Oct 2025, and that Sweden's elite forwards (Isak, Gyökeres) are
offset by poor results vs strong European sides. If Kalshi priced Japan at, say,
33¢, that 41% vs 33% = 8% edge would trigger a ~$22 half-Kelly bet.

## Data coverage
| Data | Covered now? |
|------|--------------|
| WC qualifier form (all confederations) | ✅ |
| World Cup tournament matches | ✅ |
| Squads, ages, coaches, injuries | ✅ |
| **International friendlies (incl. pre-WC warm-ups)** | ❌ not in current plan |

The friendlies gap matters most in the ~2 weeks before kickoff, when teams play
warm-up games that are the freshest form signal. Options: upgrade the SportMonks
plan to include the International Friendlies league, or supplement from another
source.
