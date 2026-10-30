# Decisions log: futility-aware admission control

Plan: https://claude.ai/artifact/LxT22SaYwRGUTc3Wr79bBm  (sections referenced as "plan §N").
Decisions Ramkiran already made are in plan §10 and are not repeated here.
Each entry: date, decision, why, alternatives considered.

---

### D1 (2026-10-30) Run Postgres 16 and Redis natively, not through docker compose
* **Decision:** Postgres 16 on port 5435 (initdb'd as a normal user, `max_connections=300`) and `redis-server` on 6390, i.e. the same ports and credentials docker-compose.yml uses, so no app setting changes.
* **Why:** the cloud container has the Docker CLI but no daemon.
* **Alternatives:** none available in this environment.
* **Consequence:** absolute numbers come from a 4-core cloud VM, not the 2-core laptop behind `docs/aimd_admission.md`. That is why P0 re-runs the baselines on this machine: new arms are only compared with baselines run here.

### D2 (2026-10-30) k6 baseline: single local API process, not the 3-replica nginx deployment
* **Decision:** k6 v0.54.0 (downloaded binary) drives `results/load/flashsale.js` against ONE local uvicorn process. `results/load/run_k6.py` is not used because it shells into docker containers (`docker exec`/`docker stats`) that do not exist here.
* **Why:** no Docker daemon (D1). The 3-replica + nginx topology cannot be reproduced in this environment.
* **Alternatives:** skip k6 entirely (loses the only harness that polls /queue/status for real); write a local multi-replica launcher (nginx is not installed; time better spent on the core arms).
* **Consequence:** the k6 numbers are NOT comparable to `results/load/summary_*.json` (3 replicas, laptop). They are a fresh single-replica baseline on this machine.

### D3 (2026-10-30) "ruff clean" means the new and changed code, not the whole repo
* **Decision:** every new file is ruff clean under ruff's default rules, and changed files gain no new findings. Existing findings in untouched code are left alone.
* **Why:** `ruff check app scripts tests` reports 151 findings on `53cf50e` before any change. Fixing them would touch many files (including near the frozen handlers in `app/main.py`) for no research value.
* **Alternatives:** fix all 151 (large unrelated diff; risk near frozen code); add a repo-wide ruff config (changes project conventions without the owner).

### D4 (2026-10-30) Prove "flags off = today's behaviour" with a golden trace
* **Decision:** `tests/flags_off_scenario.py` drives the waiting room (join, admit, status, validate, wait estimate, controller snapshot) and the door (no token, bad token, conflict then retry, confirmed, spent, other buyer, sold out) against Redis, normalises random tokens, and records responses plus the resulting Redis keyspace. The golden file `tests/golden/flags_off.json` was recorded on `53cf50e`, before any new module existed, and checked stable over repeated runs. `tests/test_flags_off.py` asserts the trace is unchanged.
* **Why:** a structural "no new keys" check would miss subtle changes (say, a TTL renewal or a header). A trace from the untouched code is the strongest cheap evidence.
* **Alternatives:** loading the old modules from git inside the test (fragile, needs git); relying on the existing suite only (it does not look at the Redis keyspace).
* **Scope:** uses Redis db 15, which it flushes. It does not exercise the admission worker thread or the database.

### D5 (2026-10-30) New harness script instead of editing the AIMD benchmark
* **Decision:** the buyer behaviour model lives in a new `scripts/fa_benchmark.py`, which imports the AIMD benchmark's plumbing. `scripts/admission_control_benchmark.py` is unchanged. For k6, the behaviour model is in a copy, `results/fa/load/flashsale_fa.js`.
* **Why:** `docs/aimd_admission.md`'s numbers must stay reproducible with the exact script that produced them, and rule 4 forbids overwriting files under `results/` (which is where `flashsale.js` lives).
* **Alternatives:** add options to the old harness with backwards-compatible defaults (larger diff to a script whose outputs are cited; easy to change behaviour by accident).

### D6 (2026-10-30) Every admitted buyer is "notified" by one real GET /queue/status
* **Decision:** in `fa_benchmark.py`, a buyer whose grant appears calls `/queue/status` once over HTTP (that is when their fixed TTL clock starts), then decides to show or not. No-shows are buyers who saw "admitted" and walked away. Applied to every arm, baselines included.
* **Why:** under `FA_TTL_MODE=fixed` the clock starts at first notification; without a status call the clock would start at the grant and the claim-window arms would get a different TTL semantics from what a real client sees. Doing it in every arm keeps the arms comparable (in renew mode the call renews the TTL once, as a real client's first poll would).
* **Alternatives:** model buyers who abandon before being told (their clock starts at the grant). Possible later as a separate behaviour option.

### D7 (2026-10-30) First-pass sizing: 200 units, 90 s deadline, lognormal show delay (median 2 s, sigma 1)
* **Decision:** stock 200 (600 buyers at ratio 3, 2,000 at ratio 10), sale deadline 90 s (three TTLs), show delay lognormal with median 2 s and sigma 1 (about 0.35% of shows would be later than the 30 s TTL). p_pay = 1 (no payment failures) in the first pass.
* **Why:** the grid is 360 runs and must finish overnight on one machine, so each run is capped at about 95 s. A 90 s deadline lets cohorts mature (first at about 30 s) so the estimator can leave its prior, which the pilot's 1.5 s TTL made trivial and the real 30 s TTL does not. The pilot's delays (0.05 to 0.3 s) are far below a 30 s TTL and would not exercise censoring at all; a lognormal with a seconds-scale median and a long tail does. Payment failures are left out because the grid as specified does not vary them and the current system treats a confirmed order as final.
* **Alternatives:** larger stock (longer runs, fewer seeds by morning); 120 s deadline (more liveness for slow arms, about 30% longer grid); the pilot's uniform delays (fidelity to the pilot, but tests nothing new).
* **Consequence:** `sold_pct` < 100 at the deadline is a liveness loss and is reported as such, not hidden.

### D8 (2026-10-30) Claim-window arms admit with no pacing cap; the DB limiter bounds concurrency
* **Decision:** the claim window admits the whole budget (W - U) in one tick, as the pilot did. The DB limiter's gate is the only bound on concurrent checkouts. A request that waits more than `DBLIM_MAX_WAIT_S` = 20 s at the gate gets 503.
* **Why:** fidelity to the pilot mechanism being evaluated, and it is the decoupling argument (plan 3.2). The pilot's high p99 from this burst is a known weakness; the harness reports gate wait separately so it is visible, not hidden.
* **Alternatives:** a pacing cap on admissions (likely better tail latency; a new mechanism that should be its own arm, not silently mixed in).

### D9 (2026-10-30) DB limiter specifics
* **Decision:** cap = 30 (`DBLIM_MAX_CWND`, the checkout pool's 10 + 20 connections); otherwise `admission_control.CFG` defaults (latency target clamp(3 x baseline, 100, 500) ms etc.); in-flight counter self-heals like the existing controller's instead of per-holder leases.
* **Why:** reusing the controller's law and defaults unchanged keeps "coupled vs decoupled" a one-variable change. Leases add complexity that a single-replica benchmark does not exercise.
* **Alternatives:** per-holder leases (needed for crash safety across replicas; noted as future work); the pilot's tighter 15 ms latency floor (would change the control law, not just its coupling).

### D10 (2026-10-30) Unknown stock means admit nobody
* **Decision:** if `fa:free` has not been published yet, the claim window admits zero for that tick.
* **Why:** it is the conservative choice (no futile admissions on no information) and lasts at most one stock refresh (0.2 s). Each run waits 0.6 s after resetting stock before releasing the herd.

### D11 (2026-10-30) Tests run against a second Postgres (5436) and Redis (6391) while benchmarks run
* **Decision:** benchmarks use 5435/6390; the test suite is run with `POSTGRES_PORT=5436 REDIS_PORT=6391`, and the slow end-to-end test (`FA_INTEGRATION=1`) uses API port 8020.
* **Why:** so that running tests during the overnight grid cannot touch the benchmark sale or its Redis keys. (They still share CPU; tests were kept out of the window while P0 baselines ran, except one quick suite run noted here.)

### D12 (2026-10-30) S-K uses K = 40 for both strategies
* **Decision:** the static arm in the first pass is `S-K:40` (40 buyers per 500 ms) for pessimistic and optimistic.
* **Why:** it had the fastest median sell-out of the static arms in the fresh P0 baselines on this machine (`results/fa/p0/`): pessimistic 18.9 s (1,500 units), optimistic 8.3 s (600 units). On the original laptop the best optimistic K was 20; here it is 40, so the choice follows this machine's data.
* **Alternatives:** K = 20 for optimistic (the documented best on the laptop; slower here: 15.1 s); tuning K for the no-show workload (would give the baseline an oracle the claim window does not have).

### D13 (2026-10-30) Sold-out notice fires when free units reach 0, not when free = 0 AND unresolved = 0
* **Decision:** `claim_window.is_sold_out(free)` is `free <= 0`. Plan 3.4 also required "no unresolved claims"; that was wrong and is corrected in the plan.
* **Why:** free = total - reserved. Once it is 0, an unresolved admitted buyer can only lose (sold out) or expire; neither returns a unit. Only a declined payment returns one, and the stock refresh then clears the flag. The smoke run at full size (`results/fa/smoke.jsonl`, first line) showed the cost of the old rule: sold out at 38 s, but 1,313 queued buyers were told only at 69 s, after the last no-show token expired.
* **Alternatives:** keep the stricter rule (30 s later verdicts for nothing); also wait for pending payments (needed only when payments can fail; flag already self-clears).

### D14 (2026-10-30) Harness caps a buyer's show delay at TTL + 1 s
* **Decision:** a buyer whose drawn delay exceeds the TTL checks out at TTL + 1 s instead (and gets the same 403 for an expired token).
* **Why:** same outcome for the buyer and the server; it stops one long-tail buyer from holding a run open until the deadline (seen in the smoke run). Saves grid time only.

### D15 (2026-10-30) First grid attempt discarded because of a DB-limiter bug; baseline rows carried over
* **What happened:** the first attempt at the first-pass grid (74 runs, kept as `results/fa/grid/firstpass_v1_gatebug.{jsonl,log}`) ran with two defects in `app/db_limiter.py`: (1) the leader passed only the waiting-room depth to `admission_control.step()`, so once the claim window had admitted everyone the controller saw "no demand" and reset its window to 2 while hundreds of requests waited at the gate; (2) every waiting request polled Redis every 5 ms, which saturated the event loop and produced gate time-outs. `cw-naive` sold as little as 51% in that attempt.
* **Decision:** fix both (commit 04b1de3: waiters at the gate count as demand; one poller per process behind a FIFO lock), add regression tests, archive the v1 files unchanged, and restart the grid. The 26 `S-K:40` and `aimd` rows from v1 are copied into the new `firstpass.jsonl` with a `carried_from` field, because those arms never touch the DB limiter and their code path is unchanged by the fix.
* **Why:** v1 numbers for the limiter arms measure the bug, not the mechanism. Re-running the baseline rows would cost about 25 minutes for identical code.
* **Caveat:** during v1, a code edit to `app/admission.py` was briefly present in the benchmark worktree (reverted within about 2 minutes; flags-off neutral), and a 9-second Protego test ran on the test instances at 17:50:22 to 17:50:31 UTC, sharing CPU with one grid run. Both affect only v1 rows; the carried baseline rows may include that one run. After that, development moved to a separate worktree.

### D16 (2026-10-30) Protego-style arm
* **Decision:** `protego:<ms>` = the existing AIMD door (`ADMISSION_CONTROL=aimd`) plus PostgreSQL `lock_timeout` set by a connect listener on the checkout engine only (`app/protego.py`). SQLSTATE 55P03 becomes 503 `{"status": "dropped"}`; the harness retries a dropped buyer like an OCC conflict and counts drops separately. Swept over 50, 100, 250 ms.
* **Why:** this is the decision already made (lock budget only, only this arm, only the checkout engine). It is "Protego-style", not Protego: there is no per-lock queueing-delay accounting or performance-isolation scheduler.

### D17 (2026-10-30) Breakwater-style arm
* **Decision:** `breakwater:<K>` = claim window off, waiting room static K, DB limiter in `breakwater` mode: credits grow by 1 per tick while the observed queueing delay at the gate plus checkout latency p90 is under target, and shrink multiplicatively by max(1 - 0.4 (d - t)/d, 0.5) above it; target = max(20 ms, 2 x best observed p90). Cap 30.
* **Why:** a server-side credit pool driven by queueing delay is Breakwater's core idea; demand speculation, client-side credits and RPC-level overcommitment do not map onto an HTTP checkout and are left out. It is a single operating point (K = 40, beta 0.4), not tuned.

### D18 (2026-10-30) P6 runs one cell only
* **Decision:** the P6 baselines run at p_show 0.7, demand ratio 10, both strategies, 5 seeds (40 runs), after the first-pass grid.
* **Why:** time. That cell is the middle of the no-show range at the high demand ratio, where admission choices matter most. The comparison with the first-pass arms is only valid in that cell.

### D19 (2026-10-30) Container restart during the grid: resumed, not restarted
* **What happened:** the cloud container restarted at about 19:05 UTC, killing the grid after 6 new runs (Postgres, Redis and all processes stopped; files on disk survived). It was found at the 20:17 check-in.
* **Decision:** restart the four service instances with the same commands and resume the grid (the harness skips cells already in `firstpass.jsonl`). A marker line was written to `firstpass.log`. Partial results are pushed at every check-in so a later restart cannot lose them.
* **Consequence:** about 75 minutes lost. The grid runs in seed-major order, so if it is not finished by morning every cell has the same number of seeds (or one fewer); the report states n per cell.
