# Local k6 baseline (single API process, this machine)

`run_local_k6.sh` drives the UNCHANGED `results/load/flashsale.js` (copied to a
scratch path because k6 writes its summary next to it) against one local
uvicorn process, N = 1,000 VUs, 60 units, queued mode, flags at defaults
(static K = 10 per 500 ms, renew TTL). See research/DECISIONS.md D2 for why it
is not the 3-replica nginx setup behind results/load/summary_*.json, so the
two are not comparable.

| file | strategy | confirmed | admitted then sold_out | version conflicts | other_failure | never admitted | DB orders / reserved |
|---|---|---|---|---|---|---|---|
| k6_queued_pessimistic_n1000.json | pessimistic | 60 | 504 | 0 | 422 | 14 | 60 / 60 |
| k6_queued_optimistic_n1000.json | optimistic | 60 | 307 | 178 | 425 | 30 | 60 / 60 |

Counts are from the k6 summaries in this folder (fs_* counters) and the
run's printed DB check. `other_failure` (about 420 per run) is a response
that is neither confirmed, sold_out nor a version conflict, and not a 403;
its cause was NOT investigated tonight (open question in
research/MORNING_REPORT.md).
