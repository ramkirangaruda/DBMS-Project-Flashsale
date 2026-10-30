# Futility-aware admission control: code, runs, and how to reproduce them

Plan: https://claude.ai/artifact/LxT22SaYwRGUTc3Wr79bBm . Decisions: `DECISIONS.md`. Overnight status: `MORNING_REPORT.md`.

## What is where

| path | what |
|---|---|
| `app/claim_window.py` | claim window `W = ceil(free / p_hat)`, unresolved set, sold-out notice, fixed TTL (`CLAIM_WINDOW`, `FA_SOLDOUT_NOTIFY`, `FA_TTL_MODE`) |
| `app/show_estimator.py` | censoring-aware p_hat (port of `research/pilot/fa_pilot.py` `Sim.p_hat`) and the naive ablation |
| `app/stock_view.py` | free units mirrored into Redis by a thread with its own one-connection engine |
| `app/db_limiter.py` | decoupled DB concurrency gate at the door: `DB_LIMITER=aimd` (admission_control.step unchanged) or `breakwater` (Breakwater-style credits) |
| `app/protego.py` | Protego-style lock budget (`PROTEGO_LOCK_TIMEOUT_MS`) on the checkout engine only |
| `app/queue.py`, `app/admission.py` | the only existing files with hooks, all behind the flags above |
| `scripts/fa_benchmark.py` | harness with the buyer behaviour model (no-shows, show delay, payment failures), arms, resumable grids |
| `scripts/fa_analyze.py` | medians and 95% bootstrap CIs from a results JSONL |
| `results/fa/` | everything generated here (P0 baselines, k6, grid, smoke) |
| `tests/test_frozen_files.py` | the course files stay byte-identical |
| `tests/test_flags_off.py` | with every new flag off, behaviour equals the pre-change trace |

All flags default to off. With them off the waiting room and door behave exactly as before (golden trace test).

## Environment used for the overnight results

* 4-core cloud VM, Python 3.13, PostgreSQL 16 and Redis 7 run natively on the docker-compose ports (5435, 6390); no Docker daemon (DECISIONS D1).
* A second Postgres (5436) and Redis (6391) for the test suite while benchmarks run (D11).

```bash
pip install -r requirements.txt pytest ruff asyncpg     # asyncpg: only for the pilot port check
python -m scripts.seed
```

## Tests

```bash
pytest                                   # unit + Redis/Postgres tests (skip without services)
FA_INTEGRATION=1 pytest tests/test_fa_integration.py   # every claim-window mode, real server, zero oversell
ruff check app/claim_window.py app/show_estimator.py app/stock_view.py app/db_limiter.py app/protego.py \
           scripts/fa_benchmark.py scripts/fa_analyze.py tests/
```

## Runs

```bash
# P0: fresh AIMD baselines (the documented commands, outputs under results/fa/p0/)
python -m scripts.admission_control_benchmark --strategy pessimistic --stock 1500 \
  --arms static:10,static:20,static:40,static:80,static:160,aimd --repeats 3 --out results/fa/p0/aimd_pessimistic_native.json
python -m scripts.admission_control_benchmark --strategy pessimistic --stock 800 --db-delay-ms 10 \
  --arms static:5,static:10,static:20,static:40,static:80,aimd --repeats 3 --out results/fa/p0/aimd_pessimistic_slowdb.json
python -m scripts.admission_control_benchmark --strategy optimistic --stock 600 \
  --arms static:10,static:20,static:40,static:80,static:160,aimd --repeats 3 --out results/fa/p0/aimd_optimistic_native.json

# P0: local k6 baseline (single API process)
K6=/path/to/k6 PY=python results/fa/k6/run_local_k6.sh pessimistic /tmp/flashsale.js $PWD/results/fa/k6/k6_queued_pessimistic_n1000.json

# P5: first-pass grid (resumable: rerun the same command to continue)
SALE_WINDOW_ENFORCED=false python -m scripts.fa_benchmark --grid firstpass
python -m scripts.fa_analyze results/fa/grid/firstpass.jsonl > results/fa/grid/firstpass_summary.md

# One cell by hand
python -m scripts.fa_benchmark --arm cw-censored --strategy pessimistic --stock 200 --ratio 10 --p-show 0.4 --seed 1
```

Arms: `S-K:<K>`, `aimd`, `cw1`, `cw-naive`, `cw-censored`, `cw-oracle`, `cw-censored-coupled`, `protego:<ms>`, `breakwater:<K>`.
