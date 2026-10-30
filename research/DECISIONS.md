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

### D2 (2026-10-30) k6 baseline
* See D6 below (filled in once tried).

### D3 (2026-10-30) "ruff clean" means the new and changed code, not the whole repo
* **Decision:** every new file is ruff clean under ruff's default rules, and changed files gain no new findings. Existing findings in untouched code are left alone.
* **Why:** `ruff check app scripts tests` reports 151 findings on `53cf50e` before any change. Fixing them would touch many files (including near the frozen handlers in `app/main.py`) for no research value.
* **Alternatives:** fix all 151 (large unrelated diff; risk near frozen code); add a repo-wide ruff config (changes project conventions without the owner).

### D4 (2026-10-30) Prove "flags off = today's behaviour" with a golden trace
* **Decision:** `tests/flags_off_scenario.py` drives the waiting room (join, admit, status, validate, wait estimate, controller snapshot) and the door (no token, bad token, conflict then retry, confirmed, spent, other buyer, sold out) against Redis, normalises random tokens, and records responses plus the resulting Redis keyspace. The golden file `tests/golden/flags_off.json` was recorded on `53cf50e`, before any new module existed, and checked stable over repeated runs. `tests/test_flags_off.py` asserts the trace is unchanged.
* **Why:** a structural "no new keys" check would miss subtle changes (say, a TTL renewal or a header). A trace from the untouched code is the strongest cheap evidence.
* **Alternatives:** loading the old modules from git inside the test (fragile, needs git); relying on the existing suite only (it does not look at the Redis keyspace).
* **Scope:** uses Redis db 15, which it flushes. It does not exercise the admission worker thread or the database.
