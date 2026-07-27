"""Compare GPT-5.5 Pro vs Claude Opus 4.8 vs Claude Sonnet 5 for a given match.

Usage:
    python scripts/compare_models.py "France" "Morocco"
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import config
from src.dossier import build_match_dossier, render_markdown
from src.sportmonks import SportMonks
from src.teams import build_name_index, resolve
from src import llm
from src.reasoning import _SYSTEM, _SCHEMA


def run(home: str, away: str):
    print(f"\nBuilding dossier for {home} vs {away}...")
    sm = SportMonks()
    idx = build_name_index(sm)
    home_id = resolve(sm, home, idx)
    away_id = resolve(sm, away, idx)
    if not home_id or not away_id:
        print(f"Could not resolve team names. home_id={home_id}, away_id={away_id}")
        sys.exit(1)

    dossier = build_match_dossier(sm, home_id, away_id)

    # Strip the head-to-head result of this exact game from both team dossiers
    # so we're not reasoning with the answer already in the data.
    from src.teams import canon_team as _canon
    for side, opponent in [("home", away), ("away", home)]:
        t = dossier[side]
        if t.get("wc_2026") and t["wc_2026"].get("results"):
            t["wc_2026"]["results"] = [
                r for r in t["wc_2026"]["results"]
                if _canon(opponent) not in _canon(r.split(" vs ")[-1].split(":")[0].strip())
            ]
            t["wc_2026"]["matches"] = len(t["wc_2026"]["results"])

    brief = render_markdown(dossier)
    print("Dossier built. Running both models...\n")

    user = (
        f"Match: {home} vs {away} at the 2026 World Cup (neutral venue — ignore any "
        f"home/away designation, there is no home-field advantage).\n\n"
        f"{brief}\n\n"
        f"Give your independent calibrated probabilities for {home} win, draw, "
        f"and {away} win, the single most important factors, and your reasoning."
    )

    PROVIDERS = [("openai", "GPT-5.5 Pro"), ("anthropic", "Opus 4.8"), ("sonnet5", "Sonnet 5")]

    results = {}
    for provider, label in PROVIDERS:
        print(f"Running {label}...")
        try:
            out = llm.complete_json(_SYSTEM, user, _SCHEMA, max_tokens=16000,
                                    effort="high", provider=provider)
            s = out["p_home"] + out["p_draw"] + out["p_away"]
            for k in ("p_home", "p_draw", "p_away"):
                out[k] = round(out[k] / s, 4)
            results[provider] = out
            print(f"  Done.")
        except Exception as e:
            print(f"  Error: {e}")
            results[provider] = None

    labels = {p: l for p, l in PROVIDERS}
    print(f"\n{'':30}", "  ".join(f"{l:>14}" for _, l in PROVIDERS))
    print("-" * 74)

    def fmt(r, key):
        return f"{r[key]:.2f}" if r and key in r else "n/a"
    def fmtp(r, key):
        return f"{r[key]:.1%}" if r and key in r else "n/a"

    for row_label, key, fn in [
        ("p_home", "p_home", fmtp), ("p_draw", "p_draw", fmtp), ("p_away", "p_away", fmtp),
        (f"xg_home ({home})", "xg_home", fmt), (f"xg_away ({away})", "xg_away", fmt),
        ("confidence", "confidence", lambda r, k: (r or {}).get(k, "n/a")),
    ]:
        vals = "  ".join(f"{fn(results.get(p), key):>14}" for p, _ in PROVIDERS)
        print(f"{row_label:<30} {vals}")

    r_gpt, r_opus, r_s5 = results.get("openai"), results.get("anthropic"), results.get("sonnet5")
    print()
    if r_opus and r_gpt:
        print(f"Opus vs GPT   xg_home: {r_opus['xg_home']-r_gpt['xg_home']:+.2f}  xg_away: {r_opus['xg_away']-r_gpt['xg_away']:+.2f}")
    if r_s5 and r_gpt:
        print(f"S5   vs GPT   xg_home: {r_s5['xg_home']-r_gpt['xg_home']:+.2f}  xg_away: {r_s5['xg_away']-r_gpt['xg_away']:+.2f}")
    if r_s5 and r_opus:
        print(f"S5   vs Opus  xg_home: {r_s5['xg_home']-r_opus['xg_home']:+.2f}  xg_away: {r_s5['xg_away']-r_opus['xg_away']:+.2f}")

    print()
    for provider, label in PROVIDERS:
        r = results.get(provider)
        if r and r.get("rationale"):
            print(f"--- {label} rationale ---")
            print(r["rationale"])
            print()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/compare_models.py \"Home Team\" \"Away Team\"")
        sys.exit(1)
    run(sys.argv[1], sys.argv[2])
