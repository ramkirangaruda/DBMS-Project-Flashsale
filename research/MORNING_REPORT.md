# Morning report: futility-aware admission, overnight run of 2026-10-30

> DRAFT while the grid runs. Tables below are regenerated from `results/fa/` at the end; this line is removed then.

Branch: `research/futility-admission` (pushed; nothing pushed to `main`, no force-push, no history rewritten).
Plan: https://claude.ai/artifact/LxT22SaYwRGUTc3Wr79bBm . Every decision with its reasons: [`DECISIONS.md`](DECISIONS.md). How to run everything: [`README.md`](README.md).

## Headline

<!-- HEADLINE -->

## What got done, by phase

**P0, guard and baselines.** `tests/test_frozen_files.py` pins `app/models.py`, `app/database.py` and the source of the three checkout handlers (SHA-256, line endings normalised); it was committed first and run before every commit. `tests/test_flags_off.py` replays a scenario against a golden trace recorded at `53cf50e` (before any new module existed) and asserts the same responses and Redis keyspace with all flags off. Fresh AIMD baselines on this machine are in `results/fa/p0/`; a single-replica k6 baseline is in `results/fa/k6/` (D2: no Docker daemon, so not the 3-replica setup).

**P1, buyer behaviour.** `scripts/fa_benchmark.py` models no-shows (Bernoulli p_show), show delay (lognormal, uniform or exponential), and payment failures (p_pay, unit released through the existing payments path). The k6 copy `results/fa/load/flashsale_fa.js` has P_SHOW and delay parameters. S-K and AIMD arms run with p_show < 1 in the grid.

**P2, mechanism.** `app/stock_view.py` (free units mirrored into Redis, own one-connection engine), `app/claim_window.py` (`CLAIM_WINDOW=fixed:<p>|oracle|naive|censored`, unresolved set, sold-out notice, `FA_TTL_MODE=fixed` with the clock started at first notification). Hooks in `app/queue.py` and `app/admission.py` only, all behind flags.

**P3, estimator.** `app/show_estimator.py` ports the pilot's `Sim.p_hat`. `tests/test_show_estimator.py` checks exact equality with the pilot's own code on 360 synthetic buyer logs (three delay distributions, three p, 40 trials), plus property tests. The naive ablation is there too.

**P4, decoupled DB limiter.** `app/db_limiter.py` gates checkouts at the door and drives its limit with `admission_control.step()` unchanged (cap 30). A bug in the first version (D15) made the first grid attempt invalid for every limiter arm; it was fixed, regression-tested, and the grid restarted.

**P5, first-pass grid.** Arms S-K:40, AIMD (coupled), CW-1, CW-naive, CW-censored, CW-oracle; pessimistic and optimistic; p_show 0.4, 0.7, 1.0; demand ratio 3 and 10; seeds 1 to 5; 200 units, 90 s deadline, TTL 30 s (D7). <!-- GRIDSTATUS -->

**P6, Protego-style and Breakwater-style.** Code done (`app/protego.py`, `DB_LIMITER=breakwater`, D16, D17). Runs at one cell only (D18). <!-- P6STATUS -->

## Results

<!-- TABLES -->

## Broken, unfinished, or uncertain

* **Two container restarts (D19).** The cloud container was reclaimed twice while the grid ran (about 19:05 and 20:24 UTC), costing about two hours. Services were restarted with the same commands and the grid resumed from its results file. That is why the grid may not have finished all five seeds; n per cell is in every table.
* **First grid attempt invalid for limiter arms (D15).** A DB-limiter bug made CW arms look much worse than they are (as low as 51% sold). Those rows are archived in `results/fa/grid/firstpass_v1_gatebug.jsonl` and not used anywhere in this report, except the `S-K:40` and `aimd` rows, which never touch the limiter and are carried over with a `carried_from` field.
* **End-to-end oversell test predates the limiter fix.** `FA_INTEGRATION=1 pytest tests/test_fa_integration.py` passed 10/10 before commit 04b1de3; it was not re-run after, to keep the CPU free for the grid. The unit tests (108) did pass after the fix, and every grid run checks the invariant itself (`invariant_ok`). Re-run it before trusting the branch.
* **k6 "other_failure".** The local single-replica k6 baseline shows about 420 requests per run classified as other failures. Not investigated. The k6 numbers are not used for any claim about the mechanism.
* **One machine, no Docker.** 4-core VM, native Postgres and Redis on the compose ports, one API process. Load generator and server share the CPU. Absolute times are not comparable with the laptop results in `docs/`.
* **Harness shortcuts.** Notification is one status GET (D6); show delay is capped at TTL + 1 s (D14); no payment failures in the first pass (D7); the claim window admits its whole budget at once with no pacing cap (D8).
* **Protego-style and Breakwater-style are approximations** (D16, D17): lock budget only, and a delay-driven credit pool at the door. Neither is a faithful reimplementation, and each runs at one untuned operating point in one cell (D18).


## The three questions that matter most

<!-- QUESTIONS -->

## Reproduce

Services as used here (D1, D11): Postgres 16 on 5435 (benchmarks) and 5436 (tests), Redis on 6390 and 6391, all on the docker-compose credentials. With Docker available, `docker compose up -d db redis` gives the 5435/6390 pair.

```bash
git checkout research/futility-admission
pip install -r requirements.txt pytest ruff asyncpg
python -m scripts.seed

# guard, flags-off proof, unit tests (tests use the second instances)
POSTGRES_PORT=5436 REDIS_PORT=6391 pytest
FA_INTEGRATION=1 POSTGRES_PORT=5436 REDIS_PORT=6391 pytest tests/test_fa_integration.py

# first-pass grid (resumable) and its tables
OTEL_SDK_DISABLED=true SALE_WINDOW_ENFORCED=false python -m scripts.fa_benchmark --grid firstpass
python -m scripts.fa_analyze results/fa/grid/firstpass.jsonl > results/fa/grid/firstpass_summary.md

# P6 baselines (one cell) and their tables
OTEL_SDK_DISABLED=true SALE_WINDOW_ENFORCED=false python -m scripts.fa_benchmark --grid p6
python -m scripts.fa_analyze results/fa/grid/p6.jsonl > results/fa/grid/p6_summary.md
```

P0 baseline and k6 commands are in [`README.md`](README.md).

