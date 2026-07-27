# World Cup 2026 — Informed Betting System

Bottom-up, reasoning-driven betting on the 2026 World Cup. The system gathers
facts with code, forms a judgment with an LLM that *reasons* over those facts,
and only compares to the market at the very end to find edge. Every bet is
human-approved before it is placed.

## Pipeline

```
SportMonks (squads, coach, form, shot-quality, injuries, H2H, context)
        │  deterministic data gathering — no probabilities decided here
        ▼
   Dossier  ──►  Reasoning layer (claude-opus-4-8, adaptive thinking)
                        │  reasons to calibrated probabilities + rationale
                        │  NEVER shown the market price (stays independent)
                        ▼
                  Edge engine  ◄── Kalshi price (cents = implied probability)
                        │  EV after fees, ½-Kelly sizing, caps
                        ▼
                  Bet sheet  ──►  you confirm  ──►  Kalshi order
```

## Two modes
- **Offline** — analyse a match (or futures market) and produce a bet sheet.
  Implemented end-to-end in `src/orchestrator.py`.
- **Online** — re-aimed to fire just **before kickoff** using the freshest
  lineup/form data (Kalshi has no live in-play soccer market). Same pipeline,
  triggered close to kickoff.

## Layout
| File | Role |
|------|------|
| `src/config.py` | IDs, bankroll, ½-Kelly, edge/cap thresholds |
| `src/sportmonks.py` | Cached SportMonks client (rate-limit safe) |
| `src/kalshi.py` | Kalshi read client (public) + RSA-signed trade client (go-live) |
| `src/dossier.py` | Assembles the per-match fact brief |
| `src/teams.py` | Nation name → SportMonks team ID |
| `src/reasoning.py` | LLM reasons through the dossier → probabilities |
| `src/edge.py` | Edge vs market + ½-Kelly bet sheet |
| `src/orchestrator.py` | Ties it together |

## Run
```bash
pip install -r requirements.txt
python -m src.orchestrator markets                 # tradeable WC match markets
python -m src.orchestrator match "Japan" "Sweden"  # dossier (+ bet sheet w/ key)
```

## Config / secrets (`.env`, never committed)
- `MONKS_API_KEY` — SportMonks (configured).
- `ANTHROPIC_API_KEY` — enables the reasoning layer.
- `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` — only needed to place real orders.

## Staking
½-Kelly, ≥4% edge after fees, ≤$30/bet on a $250 bankroll. Tunable in `config.py`.

## Status
- ✅ SportMonks + Kalshi clients (verified live)
- ✅ Dossier assembler (verified live)
- ✅ Reasoning layer (built; set `ANTHROPIC_API_KEY` to run)
- ✅ Edge engine + ½-Kelly bet sheet (verified)
- ⏳ Kalshi books open ~kickoff; bet sheets populate then
- ⏳ Order placement wired but inactive until you fund + add Kalshi keys
