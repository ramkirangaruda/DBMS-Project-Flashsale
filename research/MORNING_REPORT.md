# Morning report: futility-aware admission, overnight run of 2026-10-30

> Written at 00:40 UTC from the first-pass grid as it stood (256 of 360 runs: seeds 1 to 3 everywhere, seed 4 in most cells). The grid and then P6 are still running and will land in `results/fa/grid/`. Re-run `fa_analyze` for the final tables; the numbers below may move.

Branch: `research/futility-admission` (pushed; nothing pushed to `main`, no force-push, no history rewritten).
Plan: https://claude.ai/artifact/LxT22SaYwRGUTc3Wr79bBm . Every decision with its reasons: [`DECISIONS.md`](DECISIONS.md). How to run everything: [`README.md`](README.md).

## Headline

**The claim window does what it is meant to do on futility and costs a lot of speed.** With the censoring-aware estimator (`cw-censored`) and p_show 0.7 or 1.0, admitted buyers who lose at checkout fall from 218 to 1,791 per sale (S-K:40 and AIMD) to a median of 1 to 7. Zero oversells in all 256 runs. But it loses on every time metric in every cell:

* **Sell-out time.** S-K:40 sells out in 5.0 to 10.0 s (medians). `cw-censored` takes 32.3 to 46.0 s, `cw-oracle` 17.0 to 30.8 s. AIMD sits between (8.7 to 37.5 s).
* **Loser verdict.** Median time until a buyer who will not get a unit is told: S-K:40 7.9 to 19.6 s, `cw-censored` 32.4 to 46.1 s. The claim window beats AIMD on this only at demand ratio 10 (e.g. pessimistic p 0.7: 37.3 vs 49.3 s).
* **Checkout latency.** Client checkout p95 is 2.3 to 10.3 s for `cw-censored` and up to 28 s for `cw-naive` (gate wait from admitting the whole budget at once, D8), against 31 to 595 ms for AIMD.

Two causes are visible in the data. (1) At p 1.0 the claim window has 95% of units sold by about 13 s, then waits until about 30 s for the last few admitted buyers, whose delays are in the lognormal tail (table "time to 95% sold" in the summary). That is the price of admitting only ceil(free/p) buyers: the last units wait for the slowest admitted buyer or a 30 s TTL expiry. (2) At p 0.4 the estimator sits at its prior p = 1 until the first cohort matures, about 30 s in, so `cw-censored` behaves like `cw1` for the first 30 s.

**Where the mechanism loses outright on its own metric:** p 0.4, demand ratio 3, where `cw-censored` loses 41 admitted buyers (same as S-K:40 and AIMD) and takes about 39 s instead of 10. At p 0.4 ratio 10 it loses 251 (pessimistic) and 218 (optimistic) against AIMD's 308 and 304 and S-K's 620, with CIs that overlap AIMD's. The oracle shows what the window could do with a correct p from the start: 4 lost in both cells.

**Estimator.** At the end of each run the censoring-aware p_hat is within 0.025 of the true effective show rate in every cell. The naive estimator collapses at p 1.0 and ratio 10 (final p_hat 0.65, and the arm loses 1,061 to 1,105 admitted buyers, about the same as having no window) because it counts buyers who have not shown yet as no-shows.

**cw1 (window with p fixed at 1) is not live with no-shows:** 79.8 to 80.0% sold by the 90 s deadline at p 0.4 and about 99% at p 0.7, as expected.


## What got done, by phase

**P0, guard and baselines.** `tests/test_frozen_files.py` pins `app/models.py`, `app/database.py` and the source of the three checkout handlers (SHA-256, line endings normalised); it was committed first and run before every commit. `tests/test_flags_off.py` replays a scenario against a golden trace recorded at `53cf50e` (before any new module existed) and asserts the same responses and Redis keyspace with all flags off. Fresh AIMD baselines on this machine are in `results/fa/p0/`; a single-replica k6 baseline is in `results/fa/k6/` (D2: no Docker daemon, so not the 3-replica setup).

**P1, buyer behaviour.** `scripts/fa_benchmark.py` models no-shows (Bernoulli p_show), show delay (lognormal, uniform or exponential), and payment failures (p_pay, unit released through the existing payments path). The k6 copy `results/fa/load/flashsale_fa.js` has P_SHOW and delay parameters. S-K and AIMD arms run with p_show < 1 in the grid.

**P2, mechanism.** `app/stock_view.py` (free units mirrored into Redis, own one-connection engine), `app/claim_window.py` (`CLAIM_WINDOW=fixed:<p>|oracle|naive|censored`, unresolved set, sold-out notice, `FA_TTL_MODE=fixed` with the clock started at first notification). Hooks in `app/queue.py` and `app/admission.py` only, all behind flags.

**P3, estimator.** `app/show_estimator.py` ports the pilot's `Sim.p_hat`. `tests/test_show_estimator.py` checks exact equality with the pilot's own code on 360 synthetic buyer logs (three delay distributions, three p, 40 trials), plus property tests. The naive ablation is there too.

**P4, decoupled DB limiter.** `app/db_limiter.py` gates checkouts at the door and drives its limit with `admission_control.step()` unchanged (cap 30). A bug in the first version (D15) made the first grid attempt invalid for every limiter arm; it was fixed, regression-tested, and the grid restarted.

**P5, first-pass grid.** Arms S-K:40, AIMD (coupled), CW-1, CW-naive, CW-censored, CW-oracle; pessimistic and optimistic; p_show 0.4, 0.7, 1.0; demand ratio 3 and 10; seeds 1 to 5; 200 units, 90 s deadline, TTL 30 s (D7). At 00:40 UTC 256 of 360 runs were done; the grid continues (D19: two container restarts cost about two hours).

**P6, Protego-style and Breakwater-style.** Code done (`app/protego.py`, `DB_LIMITER=breakwater`, D16, D17). Runs at one cell only (D18). Not run yet when this was written; it starts automatically when the grid finishes, writing `results/fa/grid/p6.jsonl`. No P6 numbers are claimed here.

## Results

Full tables, every cell, with medians and 95% percentile-bootstrap CIs of the median over seeds (10,000 resamples): [`results/fa/grid/firstpass_summary.md`](../results/fa/grid/firstpass_summary.md), generated by `scripts/fa_analyze.py` from `results/fa/grid/firstpass.jsonl`. With 3 or 4 seeds per cell the CIs are coarse and can only take values seen in the data. Below, the pessimistic strategy (optimistic is in the same file and tells the same story); values copied from that file.

**Admitted, then lost at checkout (sold out), median [95% CI]**

| p_show, ratio | S-K:40 | AIMD | cw1 | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|---|
| 0.4, 3 | 41 [29, 48] | 41 [29, 48] | 0 [0, 0] | 41 [27, 47] | 41 [28, 48] | 4 [0, 15] |
| 0.4, 10 | 620 [579, 626] | 308 [273, 331] | 0 [0, 0] | 614 [578, 623] | 251 [30, 379] | 4 [0, 15] |
| 0.7, 3 | 218 [215, 230] | 218 [215, 230] | 0 [0, 0] | 217 [214, 226] | 1 [0, 15] | 5 [0, 10] |
| 0.7, 10 | 1206 [1182, 1217] | 1201 [1161, 1212] | 0 [0, 0] | 1194 [1178, 1202] | 3 [0, 13] | 4 [0, 12] |
| 1.0, 3 | 397 [395, 398] | 397 [395, 398] | 0 [0, 0] | 395 [394, 397] | 1 [0, 1] | 0 [0, 0] |
| 1.0, 10 | 1782 [1717, 1794] | 1791 [1787, 1794] | 0 [0, 0] | 1105 [1005, 1766] | 1 [0, 1] | 0 [0, 1] |

**Time to sell out (or the 90 s deadline), s**

| p_show, ratio | S-K:40 | AIMD | cw1 | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|---|
| 0.4, 3 | 10.0 [9.0, 10.6] | 37.4 [36.3, 38.7] | 90.0 [90.0, 90.0] | 12.1 [10.6, 12.8] | 39.0 [37.4, 39.3] | 18.8 [14.8, 90.0] |
| 0.4, 10 | 9.4 [8.5, 10.4] | 37.3 [36.2, 38.3] | 90.0 [90.0, 90.0] | 24.3 [22.8, 26.1] | 45.7 [39.5, 47.8] | 19.0 [15.0, 66.4] |
| 0.7, 3 | 6.2 [6.1, 6.8] | 11.2 [10.2, 12.9] | 90.0 [90.0, 90.0] | 11.6 [10.7, 12.6] | 43.8 [35.1, 90.0] | 17.0 [12.4, 27.9] |
| 0.7, 10 | 6.3 [6.0, 6.4] | 11.1 [10.0, 12.5] | 90.0 [90.0, 90.0] | 23.0 [20.7, 23.3] | 38.6 [35.9, 90.0] | 22.5 [11.1, 31.4] |
| 1.0, 3 | 5.0 [4.9, 5.2] | 8.7 [8.6, 9.3] | 36.0 [32.1, 38.0] | 11.2 [10.6, 12.6] | 32.5 [29.7, 35.5] | 30.0 [25.5, 30.8] |
| 1.0, 10 | 5.4 [5.4, 5.5] | 9.7 [8.6, 11.1] | 36.1 [32.9, 37.1] | 24.2 [17.4, 24.8] | 32.6 [30.7, 35.3] | 28.6 [25.7, 31.0] |

**Loser verdict p50, s** (when a buyer who will not get a unit learns it)

| p_show, ratio | S-K:40 | AIMD | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|
| 0.4, 3 | 12.1 [10.8, 13.1] | 39.6 [38.3, 40.4] | 14.1 [13.0, 15.5] | 40.5 [38.8, 41.3] | 17.4 [14.9, 20.4] |
| 0.4, 10 | 18.9 [18.2, 29.7] | 67.3 [64.6, 67.3] | 29.0 [27.8, 31.8] | 45.1 [39.7, 47.8] | 19.1 [15.0, 66.5] |
| 0.7, 3 | 8.4 [8.3, 9.2] | 17.0 [13.2, 31.9] | 12.8 [11.8, 14.0] | 39.4 [35.2, 48.4] | 17.1 [12.6, 27.9] |
| 0.7, 10 | 17.7 [17.5, 17.7] | 49.3 [45.3, 53.4] | 32.3 [31.6, 34.7] | 37.3 [36.0, 38.6] | 22.6 [11.2, 31.4] |
| 1.0, 3 | 7.9 [7.8, 8.1] | 12.8 [11.9, 13.7] | 13.7 [13.1, 15.3] | 32.5 [29.8, 35.5] | 30.1 [25.6, 30.8] |
| 1.0, 10 | 19.1 [17.9, 39.2] | 33.4 [24.7, 41.3] | 32.5 [28.9, 43.1] | 32.7 [30.8, 35.4] | 28.7 [25.8, 31.0] |

**Client checkout p95, ms** (includes the DB-limiter gate wait for the claim-window arms)

| p_show, ratio | S-K:40 | AIMD | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|
| 0.4, 10 | 141 [78, 10115] | 31 [22, 76] | 14022 [12219, 14346] | 9855 [4297, 11417] | 1515 [1167, 1610] |
| 0.7, 10 | 166 [154, 274] | 36 [33, 47] | 19731 [17244, 20161] | 2980 [2107, 3325] | 4032 [3983, 4195] |
| 1.0, 10 | 11040 [344, 20875] | 133 [101, 526] | 20988 [18230, 31883] | 4530 [4464, 4723] | 4690 [4483, 4744] |

Seeds per cell: 4 at p 0.4 and most p 0.7 cells, 3 elsewhere (exact n per cell in the summary). Invariant held in 256 of 256 runs. Fresh P0 baselines and the k6 baseline are in `results/fa/p0/` and `results/fa/k6/` and are not repeated here.


## Decisions taken overnight

Each has its reasons and the alternatives in [`DECISIONS.md`](DECISIONS.md). The ones that shape the numbers most are D7, D8, D12 and D13.

* D1 native Postgres/Redis (no Docker daemon). D2 k6 against one local API process. D3 ruff scope is new and changed code. D4 flags-off proof by golden trace from `53cf50e`. D5 new harness, AIMD benchmark untouched.
* D6 notification = one real status GET. D7 200 units, 90 s deadline, lognormal show delay (median 2 s, sigma 1), no payment failures. D8 claim window admits its whole budget at once; the DB limiter is the only concurrency bound. D9 limiter cap 30, controller defaults unchanged. D10 unknown stock admits nobody.
* D11 tests on separate instances. D12 S-K uses K = 40 (fastest static arm in fresh P0 on this machine). D13 sold-out notice at free = 0 (the plan's extra condition was wrong; plan corrected). D14 show delay capped at TTL + 1 s.
* D15 first grid attempt discarded (limiter bug), baseline rows carried over. D16, D17 Protego-style and Breakwater-style designs. D18 P6 in one cell. D19 container restarts, grid resumed.

## Broken, unfinished, or uncertain

* **Time metrics come from trajectories sampled about once a second**, so "time to 95% sold" can be slightly later than the sell-out time in a few cells.
* **Two container restarts (D19).** The cloud container was reclaimed twice while the grid ran (about 19:05 and 20:24 UTC), costing about two hours. Services were restarted with the same commands and the grid resumed from its results file. That is why the grid may not have finished all five seeds; n per cell is in every table.
* **First grid attempt invalid for limiter arms (D15).** A DB-limiter bug made CW arms look much worse than they are (as low as 51% sold). Those rows are archived in `results/fa/grid/firstpass_v1_gatebug.jsonl` and not used anywhere in this report, except the `S-K:40` and `aimd` rows, which never touch the limiter and are carried over with a `carried_from` field.
* **End-to-end oversell test predates the limiter fix.** `FA_INTEGRATION=1 pytest tests/test_fa_integration.py` passed 10/10 before commit 04b1de3; it was not re-run after, to keep the CPU free for the grid. The unit tests (108) did pass after the fix, and every grid run checks the invariant itself (`invariant_ok`). Re-run it before trusting the branch.
* **k6 "other_failure".** The local single-replica k6 baseline shows about 420 requests per run classified as other failures. Not investigated. The k6 numbers are not used for any claim about the mechanism.
* **One machine, no Docker.** 4-core VM, native Postgres and Redis on the compose ports, one API process. Load generator and server share the CPU. Absolute times are not comparable with the laptop results in `docs/`.
* **Harness shortcuts.** Notification is one status GET (D6); show delay is capped at TTL + 1 s (D14); no payment failures in the first pass (D7); the claim window admits its whole budget at once with no pacing cap (D8).
* **Protego-style and Breakwater-style are approximations** (D16, D17): lock budget only, and a delay-driven credit pool at the door. Neither is a faithful reimplementation, and each runs at one untuned operating point in one cell (D18).


## The three questions that matter most

1. **Is speed a goal or a constraint?** The claim window removes futile admissions but is 3 to 6 times slower to sell out than S-K:40 and gives losers their verdict later. If the paper's claim is "no futile admissions at bounded cost", we need a bound and a mechanism change to meet it, e.g. a pacing cap instead of the all-at-once burst (D8), or admitting a few buyers beyond W as the remaining units age towards the TTL. Which framing do you want before I try either?
2. **The 30 s prior period.** The estimator holds p = 1 until a cohort matures, which is most of the damage at p 0.4. Options: start from the previous sale's p_hat, a shorter TTL, or a lower-variance prior. Each changes the mechanism's claims, so this is your call.
3. **Which latency do we report?** The DB limiter keeps server checkout p95 at hundreds of ms, but client p95 (with gate wait) is seconds. Reviewers will compare with AIMD's 31 to 595 ms client p95. Do we report gate wait as part of checkout latency (as done here), or as queueing time like the waiting room?


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

