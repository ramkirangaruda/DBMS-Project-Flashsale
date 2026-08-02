"""
Batch bot-detection scoring pass over real UserBehaviorLog rows.

Deliberately a separate, explicit step from the checkout endpoints rather
than an inline per-request check:
  - Isolation Forest needs a reasonably sized batch of sessions to fit
    against and compare relative anomalousness -- there's no meaningful
    "is this one session anomalous" answer in isolation, only "anomalous
    relative to the rest of this batch".
  - accounts_per_device and requests_per_ip_per_min are both computed over
    SETS of rows (every user sharing a fingerprint; every request from an
    IP in the same minute), not knowable from a single request in real
    time without re-scanning the users/logs tables on every checkout.

Run this after a batch of checkout traffic -- e.g. after a demo burst
(python -m app.demos.demo_5_benchmark), or on a periodic schedule (a cron
job / scheduled task) in front of a real deployment.

Run with: venv/bin/python -m scripts.run_bot_scoring
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.ml.bot_detection import score_sessions


def run():
    db = SessionLocal()
    try:
        score_sessions(db)
    finally:
        db.close()


if __name__ == "__main__":
    run()
