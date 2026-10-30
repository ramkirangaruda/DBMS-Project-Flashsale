"""
With every futility-aware flag off, the waiting room and the door behave
exactly as they did before those modules existed.

The reference trace (tests/golden/flags_off.json) was recorded from commit
53cf50e, before any of app/claim_window.py, app/show_estimator.py,
app/stock_view.py or app/db_limiter.py existed. See tests/flags_off_scenario.py.
"""
import json
import os

import pytest

from tests import flags_off_scenario

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "flags_off.json")

NEW_FLAGS = ("CLAIM_WINDOW", "DB_LIMITER", "FA_TTL_MODE", "FA_SOLDOUT_NOTIFY", "PROTEGO_LOCK_TIMEOUT_MS")


def test_all_new_flags_default_off():
    for flag in NEW_FLAGS:
        assert flag not in os.environ, f"{flag} is set in the test environment"


def test_flags_off_trace_matches_pre_futility_code(redis_client):
    with open(GOLDEN) as f:
        golden = json.load(f)
    trace = json.loads(json.dumps(flags_off_scenario.run(), sort_keys=True))
    assert trace == golden


@pytest.mark.parametrize("module", ["claim_window", "show_estimator", "stock_view", "db_limiter", "protego"])
def test_new_modules_report_disabled(module):
    mod = pytest.importorskip(f"app.{module}")
    assert mod.ENABLED is False
