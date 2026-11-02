"""
The frontier: futile admissions against time to sell out, one point per arm,
the margin sweeps drawn as lines. Ramkiran's decision 1 (research/DECISIONS.md,
round 2): this is the paper's main figure.

    python -m scripts.fa_frontier results/fa/grid/frontier07.jsonl results/fa/frontier/frontier07

writes <prefix>.md (the table, medians with 95% bootstrap CIs over seeds) and
<prefix>.png / <prefix>.svg. Every number comes from the JSONL given; seed 0
(the seeding-only sale for the warm arms) is left out.
"""
import os
import statistics
import sys
from collections import defaultdict

from scripts.fa_analyze import boot_ci, load

# Categorical slots 1-3 of the reference palette (validated with the dataviz
# skill's validator; the low-contrast slots are always direct-labelled).
FAMILIES = [
    # (label, predicate on arm, colour, marker)
    ("posterior + warm G + pacing (the mechanism)", lambda a: a.startswith("cwp:") and a.endswith(":warmg:pace"),
     "#2a78d6", "o"),
    ("posterior, cold, no pacing", lambda a: a.startswith("cwp:") and a.count(":") == 1, "#eb6834", "s"),
    ("count window + warm G + pacing", lambda a: a.startswith("cwc:") and a.endswith(":warmg:pace"),
     "#1baf7a", "^"),
]
# Round-3 view (D27): the warm-p mechanism, posterior and count side by side,
# with warm-G kept as the round-2 reference line.
FAMILIES_WARMP = [
    ("posterior + warm p + pacing", lambda a: a.startswith("cwp:") and a.endswith(":warm:pace"), "#2a78d6", "o"),
    ("count window + warm p + pacing", lambda a: a.startswith("cwc:") and a.endswith(":warm:pace"),
     "#1baf7a", "^"),
    ("posterior + warm G + pacing (round 2)", lambda a: a.startswith("cwp:") and a.endswith(":warmg:pace"),
     "#eb6834", "s"),
]
INK, MUTED, GRID = "#2b2b29", "#6b6a63", "#e4e3de"


def sellout(r):
    return r["t_sellout_s"] if r["t_sellout_s"] is not None else r["elapsed_s"]


def stat(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    m = statistics.median(xs)
    lo, hi = boot_ci(xs) if len(xs) > 1 else (m, m)
    return m, lo, hi


def margin_of(arm):
    try:
        return float(arm.split(":")[1])
    except (IndexError, ValueError):
        return None


def summarise(rows):
    g = defaultdict(list)
    for r in rows:
        if r["cell"]["seed"] == 0:
            continue
        g[r["cell"]["arm"]].append(r)
    out = {}
    for arm, rs in g.items():
        w = [((r.get("a2v") or {}).get("winners") or {}) for r in rs]
        lo = [((r.get("a2v") or {}).get("losers") or {}) for r in rs]
        out[arm] = {
            "n": len(rs),
            "sold_pct": stat([r["sold_pct"] for r in rs]),
            "t": stat([sellout(r) for r in rs]),
            "futile": stat([r["futile_admitted"] for r in rs]),
            "lost": stat([r["admitted_lost"] for r in rs]),
            "admitted": stat([r["admitted"] for r in rs]),
            "verdict_queue": stat([(r["verdict_s"] or {}).get("p50") for r in rs]),
            "win_sys": stat([x.get("system_s_p50") for x in w]),
            "win_gate": stat([x.get("gate_s_mean") for x in w]),
            "win_db": stat([x.get("handler_s_mean") for x in w]),
            "lose_sys": stat([x.get("system_s_p50") for x in lo]) if any(lo) else None,
            "client_p95": stat([(r["client_latency_ms"] or {}).get("p95") for r in rs]),
            "server_p95": stat([(r.get("server_latency_ms") or {}).get("p95") for r in rs]),
            "invariant": sum(r["invariant_ok"] for r in rs),
        }
    return out


def fmt(v, dec):
    if v is None:
        return "n/a"
    m, lo, hi = v
    return f"{m:.{dec}f} [{lo:.{dec}f}, {hi:.{dec}f}]"


def table(summary, src):
    lines = [f"# Frontier from `{src}`", "",
             ("Median [95% bootstrap CI of the median] over seeds 1 to 5 (seed 0 is the seeding sale). "
              "Futile = admitted buyers who showed and got no unit (sold out at checkout, expired while "
              "waiting, or turned away at the gate). Winner/loser system time = admission to verdict minus "
              "the buyer's own show delay, p50 per run. Gate and DB = mean per winner."), "",
             ("| arm | n | sold % | time to sell out, s | futile | admitted | winner system s | "
              "winner gate s | winner DB s | loser system s | queue-loser verdict p50, s | client p95 ms | "
              "invariant |"),
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in sorted(summary, key=lambda a: (a.split(":")[0], margin_of(a) or 0, a)):
        s = summary[arm]
        lines.append(f"| {arm} | {s['n']} | {fmt(s['sold_pct'], 1)} | {fmt(s['t'], 1)} | {fmt(s['futile'], 0)} | "
                     f"{fmt(s['admitted'], 0)} | {fmt(s['win_sys'], 2)} | {fmt(s['win_gate'], 2)} | "
                     f"{fmt(s['win_db'], 3)} | {fmt(s['lose_sys'], 2)} | {fmt(s['verdict_queue'], 1)} | "
                     f"{fmt(s['client_p95'], 0)} | {s['invariant']}/{s['n']} |")
    return "\n".join(lines) + "\n"


def plot(summary, title, path_prefix, families=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.0, 5.6), dpi=150)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")

    def errs(v):
        return [[v[0] - v[1]], [v[2] - v[0]]]

    for label, pred, colour, marker in families or FAMILIES:
        arms = sorted((a for a in summary if pred(a)), key=margin_of)
        if not arms:
            continue
        xs = [summary[a]["t"][0] for a in arms]
        ys = [summary[a]["futile"][0] for a in arms]
        ax.plot(xs, ys, color=colour, lw=2, zorder=2)
        for a, x, y in zip(arms, xs, ys, strict=True):
            s = summary[a]
            ax.errorbar(x, y, xerr=errs(s["t"]), yerr=errs(s["futile"]), fmt=marker, ms=8, color=colour,
                        mec="#fcfcfb", mew=2, elinewidth=0.8, alpha=0.9, zorder=3)
            ax.annotate(f"{margin_of(a):g}", (x, y), textcoords="offset points", xytext=(6, 5),
                        fontsize=7.5, color=MUTED)
        ax.plot([], [], color=colour, marker=marker, lw=2, ms=7, label=label)

    points = [("S-K:40", "S-K:40", "D"), ("aimd", "AIMD", "v"), ("protego:100", "Protego-style 100 ms", "P"),
              ("breakwater:40", "Breakwater-style", "X"), ("cw-oracle", "count window, oracle p", "*")]
    for arm, label, marker in points:
        if arm not in summary:
            continue
        s = summary[arm]
        # Baselines are told apart by marker shape and the legend (they sit
        # on top of each other near 1,200 futile, so direct labels collide).
        ax.errorbar(s["t"][0], s["futile"][0], xerr=errs(s["t"]), yerr=errs(s["futile"]), fmt=marker, ms=9,
                    color=INK, mec="#fcfcfb", mew=1.5, elinewidth=0.8, alpha=0.9, zorder=4)
        ax.plot([], [], color=INK, marker=marker, ls="none", ms=8, label=label)

    ax.set_yscale("symlog", linthresh=10)
    ax.set_xlabel("time to sell out, s (median over 5 seeds; bars: 95% bootstrap CI)", color=INK, fontsize=9)
    ax.set_ylabel("futile admissions per sale (median)", color=INK, fontsize=9)
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    ax.grid(True, color=GRID, lw=0.8)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    if families:
        # Outside the axes: in the extra cells a baseline sits in the top right.
        fig.set_size_inches(10.4, 5.6)
        ax.legend(frameon=False, fontsize=7.5, loc="upper left", bbox_to_anchor=(1.01, 1.0), labelcolor=INK, ncol=1)
    else:
        ax.legend(frameon=False, fontsize=7.5, loc="upper right", labelcolor=INK, ncol=1)
    fig.text(0.01, 0.01, "Labels on line points are the margin. Lower-left is better.", fontsize=7.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    for ext in ("png", "svg"):
        fig.savefig(f"{path_prefix}.{ext}", facecolor=fig.get_facecolor())
    plt.close(fig)


def main(src, prefix, extra=(), families=None):
    rows = load(src)
    for path in extra:
        rows += load(path)
    summary = summarise(rows)
    os.makedirs(os.path.dirname(prefix) or ".", exist_ok=True)
    with open(prefix + ".md", "w") as f:
        f.write(table(summary, " + ".join([src, *extra])))
    c = next(r["cell"] for r in rows)
    plot(summary, f"Futile admissions vs sell-out time: {c['strategy']}, p_show {c['p_show']}, "
                  f"demand ratio {c['ratio']}, {c['stock']} units", prefix, families)
    print(f"wrote {prefix}.md, .png, .svg from {len(rows)} runs")


if __name__ == "__main__":
    # python -m scripts.fa_frontier <src.jsonl> <prefix> [--warmp extra.jsonl ...]
    args = sys.argv[1:]
    warmp = "--warmp" in args
    if warmp:
        i = args.index("--warmp")
        main(args[0], args[1], args[i + 1:], FAMILIES_WARMP)
    else:
        main(args[0], args[1])
