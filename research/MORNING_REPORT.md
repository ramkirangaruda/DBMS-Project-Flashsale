# Morning report: futility-aware admission, overnight run of 2026-10-30

> Final first-pass numbers (360 of 360 runs, 5 seeds per cell), updated 02:40 UTC. P6 results added at 03:15 UTC.

Branch: `research/futility-admission` (pushed; nothing pushed to `main`, no force-push, no history rewritten).
Plan: https://claude.ai/artifact/LxT22SaYwRGUTc3Wr79bBm . Every decision with its reasons: [`DECISIONS.md`](DECISIONS.md). How to run everything: [`README.md`](README.md).

## Headline

**The claim window does what it is meant to do on futility, and costs a lot of speed.** With the censoring-aware estimator (`cw-censored`) and p_show 0.7 or 1.0, admitted buyers who lose at checkout fall from 218 to 1,791 per sale (S-K:40 and AIMD) to a median of 0 to 2. Zero oversells in all 360 runs. But it loses on every time metric in every cell:

* **Sell-out time.** S-K:40 sells out in 5.0 to 10.2 s (medians over 5 seeds). `cw-censored` takes 31.9 to 48.3 s, `cw-oracle` 19.3 to 25.7 s, AIMD 9.3 to 37.9 s.
* **Loser verdict.** Median time until a buyer who will not get a unit is told: S-K:40 7.9 to 19.6 s, `cw-censored` 31.9 to 45.1 s. The claim window beats AIMD on this only at demand ratio 10 and p_show 0.4 or 0.7 (e.g. pessimistic p 0.7: 38.9 vs 47.5 s), and by under a second at optimistic p 1.0, ratio 10 (32.2 vs 33.1 s).
* **Checkout latency.** Client checkout p95 is 2.0 to 9.9 s for `cw-censored` and up to 27.8 s for `cw-naive` (gate wait from admitting the whole budget at once, D8), against 32 to 726 ms for AIMD.

Two causes are visible in the data. (1) At p 1.0 the claim window has 95% of units sold by about 12 to 13 s, then waits until about 32 s for the last few admitted buyers, whose show delays are in the lognormal tail (table "time to 95% sold" in the summary). That is the price of admitting only ceil(free/p) buyers: the last units wait for the slowest admitted buyer or a 30 s TTL expiry. (2) At p 0.4 the estimator sits at its prior p = 1 until the first cohort matures, about 30 s in, so `cw-censored` behaves like `cw1` for the first 30 s.

**Where the mechanism loses outright on its own metric:** p 0.4, demand ratio 3, where `cw-censored` loses 39 (pessimistic) and 29 (optimistic) admitted buyers, against 39 for S-K:40 and AIMD, with overlapping CIs, and takes about 39 to 40 s instead of 10. At p 0.4 ratio 10 it loses 172 and 151 against AIMD's 288 and S-K's 614, with CIs ([30, 379], [27, 375]) that overlap AIMD's. The oracle shows what the window could do with a correct p from the start: 3 to 4 lost in those cells.

**Estimator.** At the end of each run the censoring-aware p_hat is within 0.02 of the true effective show rate in every cell. The naive estimator collapses at p 1.0 and ratio 10 (final p_hat 0.66 and 0.72; the arm loses 1,117 and 1,216 admitted buyers, close to having no window) because it counts buyers who have not shown yet as no-shows.

**cw1 (window with p fixed at 1) is not live with no-shows:** 79.5 to 80.0% sold by the 90 s deadline at p 0.4 and 98.5% at p 0.7, as expected.

## What got done, by phase

**P0, guard and baselines.** `tests/test_frozen_files.py` pins `app/models.py`, `app/database.py` and the source of the three checkout handlers (SHA-256, line endings normalised); it was committed first and run before every commit. `tests/test_flags_off.py` replays a scenario against a golden trace recorded at `53cf50e` (before any new module existed) and asserts the same responses and Redis keyspace with all flags off. Fresh AIMD baselines on this machine are in `results/fa/p0/`; a single-replica k6 baseline is in `results/fa/k6/` (D2: no Docker daemon, so not the 3-replica setup).

**P1, buyer behaviour.** `scripts/fa_benchmark.py` models no-shows (Bernoulli p_show), show delay (lognormal, uniform or exponential), and payment failures (p_pay, unit released through the existing payments path). The k6 copy `results/fa/load/flashsale_fa.js` has P_SHOW and delay parameters. S-K and AIMD arms run with p_show < 1 in the grid.

**P2, mechanism.** `app/stock_view.py` (free units mirrored into Redis, own one-connection engine), `app/claim_window.py` (`CLAIM_WINDOW=fixed:<p>|oracle|naive|censored`, unresolved set, sold-out notice, `FA_TTL_MODE=fixed` with the clock started at first notification). Hooks in `app/queue.py` and `app/admission.py` only, all behind flags.

**P3, estimator.** `app/show_estimator.py` ports the pilot's `Sim.p_hat`. `tests/test_show_estimator.py` checks exact equality with the pilot's own code on 360 synthetic buyer logs (three delay distributions, three p, 40 trials), plus property tests. The naive ablation is there too.

**P4, decoupled DB limiter.** `app/db_limiter.py` gates checkouts at the door and drives its limit with `admission_control.step()` unchanged (cap 30). A bug in the first version (D15) made the first grid attempt invalid for every limiter arm; it was fixed, regression-tested, and the grid restarted.

**P5, first-pass grid.** Arms S-K:40, AIMD (coupled), CW-1, CW-naive, CW-censored, CW-oracle; pessimistic and optimistic; p_show 0.4, 0.7, 1.0; demand ratio 3 and 10; seeds 1 to 5; 200 units, 90 s deadline, TTL 30 s (D7). Complete: 360 of 360 runs, finished about 02:20 UTC (D19: two container restarts cost about two hours).

**P6, Protego-style and Breakwater-style.** Code done (`app/protego.py`, `DB_LIMITER=breakwater`, D16, D17). Runs at one cell only (D18). Complete: 40 runs, results below.

## Results

Full tables, every cell, with medians and 95% percentile-bootstrap CIs of the median over 5 seeds (10,000 resamples): [`results/fa/grid/firstpass_summary.md`](../results/fa/grid/firstpass_summary.md), generated by `scripts/fa_analyze.py` from `results/fa/grid/firstpass.jsonl`. With 5 seeds the CIs are coarse and can only take values seen in the data. Below, the pessimistic strategy (optimistic is in the same file and tells the same story); tables generated with the same `fmt` function.

**Admitted, then lost at checkout (sold out), median [95% CI]**

| p_show, ratio | S-K:40 | aimd | cw1 | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|---|
| 0.4, 3 | 39 [24, 48] | 39 [24, 48] | 0 [0, 0] | 39 [24, 47] | 39 [24, 48] | 3 [0, 15] |
| 0.4, 10 | 614 [562, 626] | 288 [258, 331] | 0 [0, 0] | 606 [561, 623] | 172 [30, 379] | 4 [0, 15] |
| 0.7, 3 | 218 [215, 230] | 218 [215, 230] | 0 [0, 0] | 218 [214, 226] | 0 [0, 15] | 4 [0, 10] |
| 0.7, 10 | 1199 [1175, 1217] | 1197 [1161, 1212] | 0 [0, 0] | 1192 [1162, 1202] | 2 [0, 13] | 4 [0, 12] |
| 1.0, 3 | 398 [395, 398] | 398 [395, 398] | 0 [0, 0] | 395 [394, 397] | 1 [0, 1] | 0 [0, 2] |
| 1.0, 10 | 1790 [1717, 1794] | 1791 [1787, 1795] | 0 [0, 0] | 1117 [1005, 1766] | 1 [0, 1] | 0 [0, 2] |

**Time to sell out (or the 90 s deadline), s**

| p_show, ratio | S-K:40 | aimd | cw1 | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|---|
| 0.4, 3 | 10.2 [9.0, 11.0] | 37.5 [36.3, 39.1] | 90.0 [90.0, 90.0] | 11.8 [10.6, 12.8] | 39.0 [37.4, 39.3] | 20.3 [14.8, 90.0] |
| 0.4, 10 | 9.5 [8.5, 10.4] | 37.5 [36.2, 38.7] | 90.0 [90.0, 90.0] | 23.8 [20.4, 26.1] | 45.0 [39.5, 47.8] | 19.3 [15.0, 66.4] |
| 0.7, 3 | 6.2 [6.1, 6.8] | 10.3 [10.2, 12.9] | 90.0 [90.0, 90.0] | 11.6 [10.0, 12.6] | 48.3 [35.1, 90.0] | 21.4 [12.4, 36.7] |
| 0.7, 10 | 6.3 [6.0, 6.4] | 10.5 [10.0, 12.5] | 90.0 [90.0, 90.0] | 23.0 [20.7, 24.8] | 39.1 [35.9, 90.0] | 22.5 [11.1, 36.7] |
| 1.0, 3 | 5.0 [4.9, 5.5] | 9.3 [8.6, 10.2] | 32.9 [20.3, 38.0] | 11.2 [10.4, 13.7] | 31.9 [19.7, 35.5] | 25.5 [16.9, 30.8] |
| 1.0, 10 | 5.4 [4.9, 5.5] | 9.3 [8.6, 11.1] | 32.9 [19.9, 37.1] | 24.2 [17.4, 27.1] | 31.9 [18.6, 35.3] | 25.7 [17.3, 31.0] |

**Loser verdict p50, s** (when a buyer who will not get a unit learns it; cw1 tells nobody before the deadline at p < 1)

| p_show, ratio | S-K:40 | aimd | cw1 | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|---|
| 0.4, 3 | 12.6 [10.8, 13.1] | 39.8 [38.3, 41.2] | n/a | 13.9 [12.3, 15.5] | 40.5 [38.8, 41.3] | 18.9 [14.9, 63.1] |
| 0.4, 10 | 19.1 [18.2, 29.7] | 67.3 [64.6, 68.4] | n/a | 28.1 [23.2, 31.8] | 45.1 [39.7, 47.8] | 19.4 [15.0, 66.5] |
| 0.7, 3 | 8.3 [8.1, 9.2] | 13.9 [13.0, 31.9] | n/a | 12.6 [10.9, 14.0] | 43.9 [35.2, 55.1] | 21.4 [12.6, 36.8] |
| 0.7, 10 | 17.7 [17.5, 18.0] | 47.5 [45.3, 53.4] | n/a | 32.6 [31.6, 35.3] | 38.9 [36.0, 55.6] | 22.6 [11.2, 36.7] |
| 1.0, 3 | 7.9 [7.4, 8.1] | 13.0 [11.9, 15.6] | 33.0 [20.4, 38.1] | 13.7 [13.0, 16.7] | 32.0 [19.9, 35.5] | 25.6 [16.9, 30.8] |
| 1.0, 10 | 17.9 [17.0, 39.2] | 24.7 [24.0, 41.3] | 32.9 [20.0, 37.2] | 32.5 [28.9, 43.1] | 32.0 [18.8, 35.4] | 25.8 [17.3, 31.0] |

**Client checkout p95, ms** (includes the DB-limiter gate wait for the claim-window arms)

| p_show, ratio | S-K:40 | aimd | cw1 | cw-naive | cw-censored | cw-oracle |
|---|---|---|---|---|---|---|
| 0.4, 3 | 82 [59, 134] | 63 [46, 140] | 162 [94, 1131] | 2372 [1293, 3623] | 4322 [2067, 4632] | 1453 [1115, 2213] |
| 0.4, 10 | 120 [78, 10115] | 32 [22, 76] | 192 [146, 979] | 13770 [12011, 14346] | 9837 [4297, 11417] | 1489 [1167, 1610] |
| 0.7, 3 | 153 [100, 237] | 82 [34, 323] | 2908 [1933, 3174] | 3048 [2241, 5668] | 2829 [1669, 3349] | 4250 [4089, 4511] |
| 0.7, 10 | 165 [118, 274] | 36 [33, 61] | 2822 [1777, 3041] | 19710 [15007, 20161] | 2980 [2107, 3325] | 4182 [3983, 4200] |
| 1.0, 3 | 147 [120, 163] | 726 [137, 1639] | 4385 [4296, 4681] | 4720 [4221, 8359] | 4402 [4263, 4571] | 4362 [4065, 4532] |
| 1.0, 10 | 344 [179, 20875] | 133 [99, 526] | 4529 [74, 4809] | 27331 [18230, 31883] | 4530 [4464, 4723] | 4627 [4263, 4744] |

### P6: Protego-style and Breakwater-style (pessimistic, p_show 0.7, ratio 10, 5 seeds)

From [`results/fa/grid/p6_summary.md`](../results/fa/grid/p6_summary.md) (`results/fa/grid/p6.jsonl`); first-pass rows for the same cell repeated from the tables above for comparison. Optimistic is in the P6 summary and tells the same story.

| arm | lost at checkout | time to sell out, s | loser verdict p50, s | client checkout p95, ms |
|---|---|---|---|---|
| protego:50 | 1191 [1149, 1210] | 11.6 [10.2, 12.2] | 49.3 [47.6, 51.0] | 67 [32, 166] |
| protego:100 | 1197 [1169, 1212] | 10.1 [9.9, 10.7] | 47.2 [45.2, 50.9] | 39 [39, 50] |
| protego:250 | 1197 [1147, 1212] | 10.7 [10.1, 12.8] | 47.6 [45.6, 51.0] | 38 [35, 305] |
| breakwater:40 | 1034 [938, 1165] | 18.5 [14.7, 23.6] | 28.9 [28.6, 31.0] | 15965 [12729, 23402] |
| aimd (first pass) | 1197 [1161, 1212] | 10.5 [10.0, 12.5] | 47.5 [45.3, 53.4] | 36 [33, 61] |
| cw-censored (first pass) | 2 [0, 13] | 39.1 [35.9, 90.0] | 38.9 [36.0, 55.6] | 2980 [2107, 3325] |

* **The lock budget almost never fired.** Across all 30 Protego-style runs there were 2 lock-timeout drops (one run, 50 ms, pessimistic). The AIMD door keeps checkout concurrency low enough that no checkout waits 50 ms for a row lock, so Protego-style is AIMD in this workload. The timeouts are not distinguishable from each other: 100 ms has the lowest median sell-out time (10.1 s pessimistic, 10.3 s optimistic), but every CI overlaps. Reporting "best = 100 ms" would be reading noise. Invariant held in all 40 P6 runs (lock timeouts counted as drops, not errors, as decided).
* **Breakwater-style** (no claim window, credits driven by queueing delay) shows fewer "lost at checkout" than AIMD (1,034 vs 1,197), but that is not a win: in its runs 17 to 264 admitted buyers per sale had their token expire (403) while waiting at the gate, and those are counted as expired, not lost (`outcomes` in `p6.jsonl`). It is slower than AIMD and has the worst client p95 here (16 s, gate wait). One untuned operating point (D17).
* All 30 Protego-style runs hit the 90 s deadline although they sold out in 10 to 13 s; the harness kept running until the deadline because some buyers were still unresolved. Not investigated; it does not change the sold-out time or the lost count.

5 seeds in every cell. Invariant held in 360 of 360 first-pass runs. Fresh P0 baselines and the k6 baseline are in `results/fa/p0/` and `results/fa/k6/` and are not repeated here.


## Decisions taken overnight

Each has its reasons and the alternatives in [`DECISIONS.md`](DECISIONS.md). The ones that shape the numbers most are D7, D8, D12 and D13.

* D1 native Postgres/Redis (no Docker daemon). D2 k6 against one local API process. D3 ruff scope is new and changed code. D4 flags-off proof by golden trace from `53cf50e`. D5 new harness, AIMD benchmark untouched.
* D6 notification = one real status GET. D7 200 units, 90 s deadline, lognormal show delay (median 2 s, sigma 1), no payment failures. D8 claim window admits its whole budget at once; the DB limiter is the only concurrency bound. D9 limiter cap 30, controller defaults unchanged. D10 unknown stock admits nobody.
* D11 tests on separate instances. D12 S-K uses K = 40 (fastest static arm in fresh P0 on this machine). D13 sold-out notice at free = 0 (the plan's extra condition was wrong; plan corrected). D14 show delay capped at TTL + 1 s.
* D15 first grid attempt discarded (limiter bug), baseline rows carried over. D16, D17 Protego-style and Breakwater-style designs. D18 P6 in one cell. D19 container restarts, grid resumed.

## Broken, unfinished, or uncertain

* **"Lost at checkout" excludes admitted buyers whose token expired while they waited** (counted as `expired`). Medians are 1 to 5 per run for every first-pass arm, max 7 for `cw-censored`, but up to 831 in one `cw-naive` run and 17 to 264 for Breakwater-style, so those two arms are worse than the lost column alone says.
* **Time metrics come from trajectories sampled about once a second**, so "time to 95% sold" can be slightly later than the sell-out time in a few cells.
* **Two container restarts (D19).** The cloud container was reclaimed twice while the grid ran (about 19:05 and 20:24 UTC), costing about two hours. Services were restarted with the same commands and the grid resumed from its results file. The grid still finished all five seeds.
* **First grid attempt invalid for limiter arms (D15).** A DB-limiter bug made CW arms look much worse than they are (as low as 51% sold). Those rows are archived in `results/fa/grid/firstpass_v1_gatebug.jsonl` and not used anywhere in this report, except the `S-K:40` and `aimd` rows, which never touch the limiter and are carried over with a `carried_from` field.
* **End-to-end oversell test re-run after the limiter fix:** `FA_INTEGRATION=1 pytest tests/test_fa_integration.py` passed 10 of 10 at 03:20 UTC (every claim-window mode, both strategies, zero oversell), after the grid and P6 finished. Full suite: 108 passed, 10 skipped (the integration tests without the flag). Ruff on the new and changed files: the only findings are in four pre-existing test files this branch does not touch (D3).
* **k6 "other_failure".** The local single-replica k6 baseline shows about 420 requests per run classified as other failures. Not investigated. The k6 numbers are not used for any claim about the mechanism.
* **One machine, no Docker.** 4-core VM, native Postgres and Redis on the compose ports, one API process. Load generator and server share the CPU. Absolute times are not comparable with the laptop results in `docs/`.
* **Headline numbers come from the Python harness, not k6** (plan 6.1 said k6). Each admitted buyer is notified by a real status GET (D6), so the notification path is exercised, but the k6 FA variant (`results/fa/load/flashsale_fa.js`) has not been run, and the k6 baseline itself has unexplained failures (below).
* **Harness shortcuts.** Notification is one status GET (D6); show delay is capped at TTL + 1 s (D14); no payment failures in the first pass (D7); the claim window admits its whole budget at once with no pacing cap (D8).
* **Protego-style and Breakwater-style are approximations** (D16, D17): lock budget only, and a delay-driven credit pool at the door. Neither is a faithful reimplementation, and each runs at one untuned operating point in one cell (D18).


## The three questions that matter most

1. **Is speed a goal or a constraint?** The claim window removes futile admissions but is 3 to 6 times slower to sell out than S-K:40 (31.9 to 48.3 s against 5.0 to 10.2 s) and gives losers their verdict later. If the paper's claim is "no futile admissions at bounded cost", we need a bound and a mechanism change to meet it, e.g. a pacing cap instead of the all-at-once burst (D8), or admitting a few buyers beyond W as the remaining units age towards the TTL. Which framing do you want before I try either?
2. **The 30 s prior period.** The estimator holds p = 1 until a cohort matures, which is most of the damage at p 0.4. Options: start from the previous sale's p_hat, a shorter TTL, or a lower-variance prior. Each changes the mechanism's claims, so this is your call.
3. **Which latency do we report?** The DB limiter keeps server checkout p95 at hundreds of ms, but client p95 (with gate wait) is seconds. Reviewers will compare with AIMD's 32 to 726 ms client p95. Do we report gate wait as part of checkout latency (as done here), or as queueing time like the waiting room?


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

