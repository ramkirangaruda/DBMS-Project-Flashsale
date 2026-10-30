"""
Tables for the futility-aware admission results: medians with 95% bootstrap
confidence intervals, per (strategy, p_show, demand ratio, arm).

    python -m scripts.fa_analyze results/fa/grid/firstpass.jsonl > results/fa/grid/firstpass_summary.md

Every number printed comes from the JSONL file given; nothing is rounded in
anyone's favour beyond the stated decimals. The bootstrap is a percentile
bootstrap of the median over seeds (10,000 resamples, fixed RNG seed), so
with 5 seeds the interval is coarse and says so: it can only take values
that occur in the data.
"""
import json
import random
import statistics
import sys
from collections import defaultdict

ARM_ORDER = ["S-K:40", "S-K:20", "aimd", "cw1", "cw-naive", "cw-censored", "cw-oracle",
             "cw-censored-coupled"]

METRICS = [
    # (column title, extractor, decimals)
    ("sold %", lambda r: r["sold_pct"], 1),
    ("time to sell out or deadline, s", lambda r: r["t_sellout_s"] if r["t_sellout_s"] is not None else r["elapsed_s"], 1),
    ("admitted, lost (sold out at checkout)", lambda r: r["admitted_lost"], 0),
    ("admitted", lambda r: r["admitted"], 0),
    ("loser verdict p50, s", lambda r: (r["verdict_s"] or {}).get("p50"), 1),
    ("OCC retries", lambda r: r["occ_retries"], 0),
    ("client checkout p95, ms", lambda r: (r["client_latency_ms"] or {}).get("p95"), 0),
    ("server checkout p95, ms", lambda r: (r["server_latency_ms"] or {}).get("p95"), 0),
]


def boot_ci(xs, n=10000, seed=12345):
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(xs, k=len(xs))) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n) - 1]


def fmt(xs, dec):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n/a"
    m = statistics.median(xs)
    if len(xs) == 1:
        return f"{m:.{dec}f} (n=1)"
    lo, hi = boot_ci(xs)
    return f"{m:.{dec}f} [{lo:.{dec}f}, {hi:.{dec}f}]"


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main(path):
    rows = load(path)
    groups = defaultdict(list)
    for r in rows:
        c = r["cell"]
        groups[(c["strategy"], c["p_show"], c["ratio"], c["arm"])].append(r)
    print(f"# Results from `{path}`\n")
    print(f"{len(rows)} runs. Invariant (never oversell) held in "
          f"{sum(r['invariant_ok'] for r in rows)} of {len(rows)}. "
          f"Runs that hit the deadline: {sum(r['hit_deadline'] for r in rows)}.\n")
    print("Cells show median [95% bootstrap CI of the median] over seeds; n = seeds per cell.\n")
    for strategy in ("pessimistic", "optimistic"):
        for p in sorted({k[1] for k in groups}):
            for ratio in sorted({k[2] for k in groups}):
                present = {k[3] for k in groups if k[:3] == (strategy, p, ratio)}
                arms = [a for a in ARM_ORDER if a in present] + sorted(present - set(ARM_ORDER))
                if not arms:
                    continue
                any_r = groups[(strategy, p, ratio, arms[0])][0]
                print(f"## {strategy}, p_show = {p}, demand ratio = {ratio} "
                      f"({any_r['buyers']} buyers, {any_r['stock']} units)\n")
                print("| arm | n | " + " | ".join(m[0] for m in METRICS) + " |")
                print("|---|---|" + "---|" * len(METRICS))
                for a in arms:
                    rs = groups[(strategy, p, ratio, a)]
                    cells = [fmt([f(r) for r in rs], d) for _, f, d in METRICS]
                    print(f"| {a} | {len(rs)} | " + " | ".join(cells) + " |")
                print()
    # p_hat accuracy for the estimating arms, at the end of each run
    print("## Estimator: final p_hat against the true effective show rate\n")
    print("| strategy | p_show | ratio | arm | n | final p_hat | true p |")
    print("|---|---|---|---|---|---|---|")
    for key in sorted(groups):
        strategy, p, ratio, a = key
        if a not in ("cw-naive", "cw-censored"):
            continue
        rs = groups[key]
        ph = [((r.get("cw_final") or {}).get("state") or {}).get("p_hat") for r in rs]
        print(f"| {strategy} | {p} | {ratio} | {a} | {len(rs)} | {fmt(ph, 3)} | {rs[0]['oracle_p']} |")


if __name__ == "__main__":
    main(sys.argv[1])
