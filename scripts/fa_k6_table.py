"""
Table for the round-2 k6 runs (research/DECISIONS.md D25, D26).

    python -m scripts.fa_k6_table results/fa/k6/round2.jsonl > results/fa/k6/round2_summary.md

One row per arm: the median over repetitions, with the min and max in
brackets (3 repetitions is too few for a bootstrap). Every number comes from
the JSONL given; the per-run values come from k6's own summary (Trend
med / p(95)) and the database check in the runner.
"""
import json
import statistics
import sys

ORDER = ["cwp:1.0", "S-K:40", "aimd", "cw-censored", "cwp:1.25:warm:pace", "cwp:1.25:warmg:pace"]
COLS = [
    # (title, extractor, decimals)
    ("confirmed", lambda j: j["confirmed"], 0),
    ("futile: admitted, sold out at checkout", lambda j: j["sold_out"], 0),
    ("told sold out in the queue", lambda j: j["told_sold_out"], 0),
    ("no-show (admitted)", lambda j: j["noshow"], 0),
    ("other failures", lambda j: j["other_failure"], 0),
    ("winner admission-to-verdict p50, ms", lambda j: j["a2v_winner_ms"]["med"], 0),
    ("admitted-loser admission-to-verdict p50, ms", lambda j: j["a2v_loser_ms"]["med"], 0),
    ("gate wait p50, ms", lambda j: j["gate_ms"]["med"], 0),
    ("gate wait p95, ms", lambda j: j["gate_ms"]["p(95)"], 0),
    ("handler (DB) p50, ms", lambda j: j["handler_ms"]["med"], 0),
    ("checkout p95 (client), ms", lambda j: j["checkout_ms"]["p(95)"], 0),
    ("queue-loser verdict p50, ms", lambda j: j["loser_verdict_ms"]["med"], 0),
]


def cell(xs, dec):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n/a"
    m = statistics.median(xs)
    return f"{m:.{dec}f} [{min(xs):.{dec}f}, {max(xs):.{dec}f}]"


def main(path):
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    arms = [a for a in ORDER if any(r["arm"] == a for r in rows)]
    arms += sorted({r["arm"] for r in rows} - set(arms))
    print(f"# k6 round 2 from `{path}`\n")
    print(f"{len(rows)} runs. 1,000 virtual buyers, 60 units, pessimistic, p_show 0.7, lognormal show delay "
          f"(median 2 s, sigma 1), one local API process. Invariant (reserved <= total and orders = reserved) "
          f"held in {sum(r['invariant_ok'] and r['db_orders_created'] == r['db_reserved'] for r in rows)} "
          f"of {len(rows)}. Cells: median [min, max] over repetitions. `cwp:1.0` is the cold seeding sale "
          f"whose p_hat and show delays seed the warm arms of the same repetition.\n")
    print("| arm | n | " + " | ".join(c[0] for c in COLS) + " |")
    print("|---|---|" + "---|" * len(COLS))
    for a in arms:
        rs = [r for r in rows if r["arm"] == a]
        print(f"| {a} | {len(rs)} | " + " | ".join(cell([f(r) for r in rs], d) for _, f, d in COLS) + " |")
    kinds = {}
    for r in rows:
        for k, v in r["other_failure_kinds"].items():
            kinds[k] = kinds.get(k, 0) + v
    print(f"\nOther-failure kinds over all runs: {kinds or 'none'}")


if __name__ == "__main__":
    main(sys.argv[1])
