"""
Round-3 tables (research/DECISIONS.md D27): each round-3 arm next to its
counterpart from the frontier grid of the same p_show.

    python -m scripts.fa_round3 > results/fa/grid/round3_summary.md

Reads results/fa/grid/frontier{07,04}.jsonl and round3_{07,04}.jsonl.
Medians [95% bootstrap CI of the median] over seeds 1 to 5, computed by
scripts/fa_frontier.summarise; every number comes from those files.
"""
import os

from scripts.fa_analyze import load
from scripts.fa_frontier import fmt, summarise

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID = os.path.join(ROOT, "results", "fa", "grid")


def row(label, arm, s):
    if arm not in s:
        return f"| {label} | `{arm}` | not run | | | | |"
    x = s[arm]
    return (f"| {label} | `{arm}` | {x['n']} | {fmt(x['sold_pct'], 1)} | {fmt(x['t'], 1)} | {fmt(x['futile'], 0)} | "
            f"{fmt(x['admitted'], 0)} |")


HEAD = ["| | arm | n | sold % | time to sell out, s | futile | admitted |", "|---|---|---|---|---|---|---|"]


def main():
    print("# Round 3: weighting isolated, and wrong priors\n")
    print("Pessimistic, demand ratio 10, 2,000 buyers, 200 units. Median [95% bootstrap CI of the median] over "
          "seeds 1 to 5. Frontier rows are from `frontier07.jsonl` / `frontier04.jsonl`, round-3 rows from "
          "`round3_07.jsonl` / `round3_04.jsonl`. Warm arms in both use the same priors (D27).\n")
    for tag, other in (("07", "04"), ("04", "07")):
        fr = summarise(load(os.path.join(GRID, f"frontier{tag}.jsonl")))
        path = os.path.join(GRID, f"round3_{tag}.jsonl")
        r3 = summarise(load(path)) if os.path.exists(path) else {}
        both = {**fr, **r3}
        p = f"0.{tag[1]}"
        print(f"## p_show {p}\n")
        print("### Posterior vs count weighting, same warm-p start and pacing\n")
        print("\n".join(HEAD))
        for m in (1.0, 1.25, 1.5, 2.0):
            print(row(f"count, margin {m:g}", f"cwc:{m}:warm:pace", both))
            print(row(f"posterior, margin {m:g}", f"cwp:{m}:warm:pace", both))
        print(f"\n### Right prior (from a p {p} sale) vs wrong prior (from a p 0.{other[1]} sale)\n")
        print("\n".join(HEAD))
        for start in ("warm", "warmg"):
            for m in (1.25, 2.0):
                print(row(f"{start}, margin {m:g}, right prior", f"cwp:{m}:{start}:pace", both))
                print(row(f"{start}, margin {m:g}, wrong prior", f"cwp:{m}:{start}:pace:from{other}", both))
        print()


if __name__ == "__main__":
    main()
