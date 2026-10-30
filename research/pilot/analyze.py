import json, statistics, sys
from collections import defaultdict

rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
g = defaultdict(list)
for r in rows:
    g[(r["cc"], r["p_show"], r["policy"])].append(r)

order = ["rate:10+cl", "rate:20+cl", "rate:40+cl", "aimd", "stock:1+cl", "stock:2+cl", "stock:3+cl", "fa+cl", "fa2+cl"]
med = lambda xs: statistics.median(xs)
print(f"runs={len(rows)}  oversold_any={any(r['oversold'] for r in rows)}  "
      f"db_paid_mismatch={sum(r['db_paid'] != r['sold'] for r in rows)}")
for cc in ("optimistic", "pessimistic"):
    for ps in (0.4, 0.7, 1.0):
        print(f"\n## {cc}, p_show={ps}   (median of seeds; time = sell-out, or elapsed if not sold out)")
        print(f"{'policy':<12}{'n':>3}{'sold%':>7}{'time_s':>8}{'lost':>7}{'futile%':>9}{'db_txns':>9}{'p99_ms':>9}")
        for pol in order:
            rs = g.get((cc, ps, pol))
            if not rs:
                continue
            t = [r["t_sellout"] if r["t_sellout"] is not None else r["elapsed"] for r in rs]
            print(f"{pol:<12}{len(rs):>3}{med([r['sold_pct'] for r in rs]):>7.1f}{med(t):>8.2f}"
                  f"{med([r['admitted_lost'] for r in rs]):>7.0f}{med([r['futile_pct'] for r in rs]):>9.1f}"
                  f"{med([r['db_txns'] for r in rs]):>9.0f}{med([r['checkout_p99_ms'] for r in rs]):>9.0f}")
