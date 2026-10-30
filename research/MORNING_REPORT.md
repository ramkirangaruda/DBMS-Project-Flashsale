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

<!-- ISSUES -->

## The three questions that matter most

<!-- QUESTIONS -->

## Reproduce

<!-- REPRODUCE -->
