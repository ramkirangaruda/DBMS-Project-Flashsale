# Futility-aware admission: pilot

Standalone pilot, separate from the app. Real PostgreSQL 16, one hot inventory
row, 2,000 buyers, 150 units. Admitted buyers may not show up (token expires
after TTL = 1.5 s) and 20% of payments fail and return the unit.

- `fa_pilot.py` : harness. Policies: `raw`, `rate:K`, `win:W`, `aimd` (coupled
  window, like app/admission_control.py), `stock:b`, `fa` (claim window
  ceil(free / p_hat) with the censoring-aware estimator), `fa2` (release-aware
  variant). `--cl` adds a separate AIMD limit on concurrent DB checkouts.
- `sweep.jsonl` : 162 runs = 9 policies x {optimistic, pessimistic} x
  p_show {0.4, 0.7, 1.0} x seeds {1, 2, 3}. Zero oversells; DB paid counts match.
- `analyze.py` : `python analyze.py sweep.jsonl` prints median tables.

Expects Postgres on a Unix socket at /tmp, port 5440 (edit `DSN` to change).
Example: `python fa_pilot.py --policy fa --cl --cc optimistic --p-show 0.4 --seed 1`

Limits: one 2-core machine, Python asyncio harness (not the app), synthetic
buyer behaviour, compressed timescales, 3 seeds. Pilot evidence only.
