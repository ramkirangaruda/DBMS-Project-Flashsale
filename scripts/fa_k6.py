"""
k6 runs of the futility-aware arms (round 2, research/DECISIONS.md D25).

k6 is the harness where every buyer polls GET /queue/status for real, so the
notification path is exercised end to end. Each run drives
results/fa/load/flashsale_fa.js through results/fa/k6/run_local_k6.sh (one
local API process, 60 units, 1,000 virtual buyers) with the arm's flags in
the server's environment.

    python -m scripts.fa_k6 --reps 3

Warm arms take their prior from a cold seeding sale (cwp:1.0, same workload)
run just before them in the same repetition. Writes one k6 summary per run
under results/fa/k6/round2/ and one JSONL line per run with the counters,
timings and the database check. Resumable: runs already in the JSONL are skipped.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.redisconf import make_redis_client
from scripts.fa_benchmark import arm_env

SALE = "22222222-2222-2222-2222-222222222222"
OUT_DIR = os.path.join(ROOT, "results", "fa", "k6", "round2")
WORKLOAD = ["-e", "P_SHOW=0.7", "-e", "DELAY_MEDIAN=2", "-e", "DELAY_SIGMA=1"]
SEED_ARM = "cwp:1.0"
ARMS = ["S-K:40", "aimd", "cw-censored", "cwp:1.25:warm:pace", "cwp:1.25:warmg:pace"]
CLEARED = ("CLAIM_WINDOW", "DB_LIMITER", "FA_TTL_MODE", "FA_SOLDOUT_NOTIFY", "FA_ORACLE_P", "ADMISSION_CONTROL",
           "PROTEGO_LOCK_TIMEOUT_MS", "FA_WEIGHTING", "FA_MARGIN", "FA_PRIOR_P", "FA_PRIOR_DELAYS",
           "FA_PACING", "FA_TIMING_HEADERS", "QUEUE_ADMIT_BATCH")


def prior_from_redis(r):
    t0 = r.hgetall(f"fa:t0:{SALE}")
    shows = r.hgetall(f"fa:show:{SALE}")
    raw = r.get(f"fa:state:{SALE}")
    state = json.loads(raw) if raw else {}
    delays = sorted(round(float(shows[k]) - float(t0[k]), 3) for k in shows if k in t0)
    return {"p_hat": state.get("p_hat"), "delays": delays}


def counters(summary):
    m = summary.get("metrics", {})

    def c(name):
        return int(((m.get(name) or {}).get("values") or {}).get("count", 0))

    def t(name, stat):
        v = ((m.get(name) or {}).get("values") or {}).get(stat)
        return None if v is None else round(v, 1)

    out = {k: c("fs_" + k) for k in ("confirmed", "sold_out", "version_conflict", "refused_403", "other_failure",
                                     "never_admitted", "join_failed", "noshow", "told_sold_out")}
    for name in ("checkout_ms", "loser_verdict_ms", "a2v_winner_ms", "a2v_loser_ms", "gate_ms", "handler_ms",
                 "queue_wait_ms"):
        out[name] = {s: t("fs_" + name, s) for s in ("med", "p(95)", "avg")}
    return out


def run(arm, rep, k6, py, prior):
    os.makedirs(OUT_DIR, exist_ok=True)
    tag = f"{arm.replace(':', '_')}_rep{rep}"
    summary_path = os.path.join(OUT_DIR, f"k6_{tag}.json")
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    env.update(arm_env(arm, None, prior))
    env.setdefault("ADMISSION_CONTROL", "static")
    env["K6"], env["PY"] = k6, py
    script = "/tmp/claude-0/k6fa/flashsale_fa.js"
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(os.path.join(ROOT, "results", "fa", "load", "flashsale_fa.js")) as f, open(script, "w") as g:
        g.write(f.read())
    proc = subprocess.run(["bash", os.path.join(ROOT, "results", "fa", "k6", "run_local_k6.sh"), "pessimistic",
                           script, summary_path, *WORKLOAD], cwd=ROOT, env=env, capture_output=True, text=True,
                          check=False)
    text = proc.stdout + proc.stderr
    m = re.search(r"orders created: (\d+); inventory reserved\|total: (\d+)\|(\d+)", text)
    others = {}
    for line in text.splitlines():
        hit = re.search(r"OTHER_FAILURE (\d+) (\S*) ?(.*?)\"?\s*source=", line) or re.search(
            r"OTHER_FAILURE (\d+) (\S*) ?(.*)$", line)
        if hit:
            key = f"{hit.group(1)} {hit.group(3)[:60]}".strip()
            others[key] = others.get(key, 0) + 1
    with open(summary_path) as f:
        summary = json.load(f)
    row = {"arm": arm, "rep": rep, "exit": proc.returncode, **counters(summary), "other_failure_kinds": others,
           "db_orders_created": int(m.group(1)) if m else None,
           "db_reserved": int(m.group(2)) if m else None, "db_total": int(m.group(3)) if m else None,
           "prior_in": {"p_hat": prior["p_hat"], "n_delays": len(prior["delays"])} if prior else None,
           "server_env": {k: v for k, v in env.items() if k in CLEARED}}
    row["invariant_ok"] = m is not None and int(m.group(2)) <= int(m.group(3))
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--k6", default=os.getenv("K6", "/home/claude/k6-v0.54.0-linux-amd64/k6"))
    p.add_argument("--py", default=sys.executable)
    p.add_argument("--out", default=os.path.join(ROOT, "results", "fa", "k6", "round2.jsonl"))
    args = p.parse_args()
    done = set()
    if os.path.exists(args.out):
        with open(args.out) as f:
            done = {(j["arm"], j["rep"]) for j in map(json.loads, filter(str.strip, f))}
    r = make_redis_client()
    for rep in range(1, args.reps + 1):
        prior = None
        for arm in [SEED_ARM, *ARMS]:
            needs = ":warm" in arm
            if arm == SEED_ARM:
                # The seeding sale must run right before the warm arms that use it.
                if (arm, rep) in done and all((a, rep) in done for a in ARMS if ":warm" in a):
                    continue
            elif (arm, rep) in done:
                continue
            row = run(arm, rep, args.k6, args.py, prior if needs else None)
            if arm == SEED_ARM:
                pr = prior_from_redis(r)
                path = os.path.join(OUT_DIR, f"prior_rep{rep}.json")
                with open(path, "w") as f:
                    json.dump(pr["delays"], f)
                prior = {**pr, "delays_path": path, "from": f"{SEED_ARM} rep {rep}"}
            if (arm, rep) not in done:
                with open(args.out, "a") as f:
                    f.write(json.dumps(row) + "\n")
                done.add((arm, rep))
            print(f"rep {rep} {arm}: confirmed {row['confirmed']} sold_out {row['sold_out']} "
                  f"told {row['told_sold_out']} other {row['other_failure']} db {row['db_reserved']}/{row['db_total']}",
                  flush=True)


if __name__ == "__main__":
    main()
