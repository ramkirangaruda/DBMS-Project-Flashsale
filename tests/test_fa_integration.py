"""
End to end: every futility-aware mode sells through the real server, door and
checkout handlers without ever overselling.

Each case starts a real API process (scripts/fa_benchmark.py) on its own port
with a short TTL so a cohort matures inside the test, runs a small herd with
no-shows, and checks the stock invariant in PostgreSQL. Slow (about a minute
per mode), so it only runs with FA_INTEGRATION=1, and it SKIPS without
Postgres and Redis like the other integration tests.

    FA_INTEGRATION=1 pytest tests/test_fa_integration.py
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(os.getenv("FA_INTEGRATION") != "1",
                                reason="slow end-to-end test; set FA_INTEGRATION=1")


@pytest.mark.parametrize("arm", ["cw1", "cw-naive", "cw-censored", "cw-oracle", "cw-censored-coupled"])
@pytest.mark.parametrize("strategy", ["pessimistic", "optimistic"])
def test_mode_never_oversells(arm, strategy, db_engine, redis_client, tmp_path):
    out = tmp_path / "run.jsonl"
    env = dict(os.environ, BENCH_BASE=os.getenv("FA_INTEGRATION_BASE", "http://127.0.0.1:8020"))
    cmd = [sys.executable, "-m", "scripts.fa_benchmark", "--arm", arm, "--strategy", strategy,
           "--stock", "20", "--ratio", "4", "--p-show", "0.6", "--delay", "uniform:0.05,1.5",
           "--ttl", "4", "--deadline", "40", "--seed", "3", "--out", str(out)]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    res = json.loads(out.read_text().splitlines()[-1])
    assert res["invariant_ok"], res
    assert res["db_reserved_stock"] <= res["db_total_stock"]
    assert res["sold"] <= 20
    assert res["db_orders_created"] == res["sold"]
    assert res["outcomes"].get("error_5xx", 0) == 0
    # It also has to actually sell: 80 buyers x 0.6 show rate for 20 units.
    assert res["sold"] == 20, res["outcomes"]
