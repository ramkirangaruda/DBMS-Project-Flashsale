"""
Section 10.2 -- Bot/Scalper Detection

Framed deliberately as UNSUPERVISED anomaly detection (Isolation Forest),
not supervised classification -- because real labeled bot-purchase data is
scarce. This is worth stating explicitly in the report: it shows you chose
the right tool for the data you actually have, rather than forcing a
classifier onto data you don't.

Features used (all pulled from UserBehaviorLog in the real schema):
  - checkout_latency_ms: time between page load and checkout completion
    (bots are often unnaturally fast)
  - session_duration_ms: how long the user browsed before buying
    (bots often skip browsing entirely)
  - accounts_per_device: how many accounts share this device fingerprint
    (the classic sign of scalper account farms)
  - requests_per_ip_per_min: request rate from the same IP

Run with: venv/bin/python -m app.ml.bot_detection

score_sessions(db) below is the real-data counterpart: it scores actual
UserBehaviorLog rows (written by app/main.py's checkout endpoints) instead
of this synthetic generator, and writes results back to FlaggedOrder /
Order.status. Run it via scripts/run_bot_scoring.py, not inline in a
checkout request -- see that script's docstring for why.
"""
import numpy as np
import pandas as pd
from sqlalchemy import text
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler


def generate_session_data(n_humans=900, n_bots=100, seed=7):
    rng = np.random.default_rng(seed)

    # Humans: slower, more varied checkout behavior, mostly 1 account per device
    human = pd.DataFrame({
        "checkout_latency_ms": rng.normal(8000, 3000, n_humans).clip(500, None),
        "session_duration_ms": rng.normal(45000, 20000, n_humans).clip(1000, None),
        "accounts_per_device": rng.choice([1, 1, 1, 1, 2], n_humans),
        "requests_per_ip_per_min": rng.poisson(2, n_humans),
    })

    # Bots: unnaturally fast checkout, near-zero browsing, many accounts per
    # device, high request rate -- the classic scalper-bot signature.
    bot = pd.DataFrame({
        "checkout_latency_ms": rng.normal(200, 80, n_bots).clip(20, None),
        "session_duration_ms": rng.normal(300, 150, n_bots).clip(10, None),
        "accounts_per_device": rng.integers(5, 40, n_bots),
        "requests_per_ip_per_min": rng.poisson(30, n_bots),
    })

    human["is_actually_bot"] = 0  # ground truth kept ONLY for evaluating the demo, not used in training
    bot["is_actually_bot"] = 1

    df = pd.concat([human, bot], ignore_index=True).sample(frac=1, random_state=seed).reset_index(drop=True)
    return df


def train_and_evaluate():
    df = generate_session_data()
    feature_cols = ["checkout_latency_ms", "session_duration_ms", "accounts_per_device", "requests_per_ip_per_min"]
    X = df[feature_cols]

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # contamination = expected proportion of anomalies; here we know it's
    # ~10% because we generated it, but in production this would be set from
    # domain knowledge/historical takedown rates, not ground truth.
    model = IsolationForest(n_estimators=200, contamination=0.1, random_state=42)
    model.fit(X_scaled)
    # IsolationForest.predict returns 1 for inliers, -1 for outliers/anomalies.
    raw_pred = model.predict(X_scaled)
    df["flagged"] = (raw_pred == -1).astype(int)
    # decision_function: lower (more negative) = more anomalous; useful as a
    # continuous score for ranking/thresholding instead of the binary flag.
    df["anomaly_score"] = -model.decision_function(X_scaled)

    flagged = df[df["flagged"] == 1]
    true_bots_caught = flagged["is_actually_bot"].sum()
    total_bots = df["is_actually_bot"].sum()
    false_positives = (flagged["is_actually_bot"] == 0).sum()

    print("Bot/Scalper Detection Model (Isolation Forest, unsupervised)")
    print("=" * 60)
    print(f"Total sessions: {len(df)}  (bots: {total_bots}, humans: {len(df) - total_bots})")
    print(f"Flagged as anomalous: {len(flagged)}")
    print(f"  True bots caught:   {true_bots_caught} / {total_bots}  "
          f"(recall = {true_bots_caught/total_bots:.1%})")
    print(f"  False positives:    {false_positives}  "
          f"(precision = {true_bots_caught/len(flagged):.1%})")

    print("\nSample flagged sessions:")
    print(flagged[feature_cols + ["is_actually_bot"]].head(6).to_string(index=False))

    print("\nWhy unsupervised: real bot-purchase labels are scarce in practice,")
    print("so Isolation Forest learns what 'normal' looks like and flags deviation,")
    print("rather than requiring a labeled training set we don't actually have.")
    print("The 'is_actually_bot' column above exists ONLY to evaluate this demo --")
    print("it was never shown to the model during training.")


def score_sessions(db, contamination=0.25, min_sessions=20):
    """
    Batch-scores REAL UserBehaviorLog rows (as opposed to train_and_evaluate()'s
    synthetic demo data), and writes results back to FlaggedOrder / Order.status.

    Feature engineering, computed from actual data instead of synthetic columns:
      - checkout_latency_ms:     checkout_time - page_load_time, per session
      - session_duration_ms:     as logged (falls back to checkout_latency_ms
                                  if a row predates that column being populated)
      - accounts_per_device:     COUNT(*) of ALL users sharing that user's
                                  device_fingerprint (from the full users table,
                                  not just users with a logged session -- the
                                  same signal the Section 6 hash index exists
                                  to look up quickly)
      - requests_per_ip_per_min: how many logged checkouts came from the same
                                  ip_address in the same one-minute bucket as
                                  this session (a bucketed count, not a sliding
                                  window -- a reasonable proxy at demo scale)

    Only sessions with a non-null order_id (i.e. the checkout actually
    succeeded) can be flagged -- there's no Order to attach a FlaggedOrder
    row to otherwise. Rejected/failed attempts are still scored as part of
    the batch (their behavior contributes to what "normal" looks like) but
    can't themselves be flagged in FlaggedOrder.

    `contamination` defaults higher here (0.25) than in train_and_evaluate()'s
    synthetic harness (0.1), because it means different things in the two
    places. The synthetic set is 10% bots BY CONSTRUCTION, so 0.1 is exactly
    right there. Real scored batches are not: a demo_5_benchmark burst is ~30%
    shared-device-cluster sessions (12 of 40), and contamination is a hard cap
    on how many rows IsolationForest will label anomalous. Leaving it at 0.1
    capped this path at 4 flags over a 40-session batch while only ~5 sessions
    in that batch have an order_id at all, so whether any flag landed on a
    flaggable session was close to a coin toss -- the pass would routinely
    write zero FlaggedOrder rows and leave GET /flagged-orders empty. 0.25
    sizes the cap to the traffic actually being scored.
    """
    query = """
        SELECT
            ubl.id AS log_id, ubl.user_id, ubl.order_id, ubl.ip_address,
            ubl.page_load_time, ubl.checkout_time, ubl.session_duration_ms,
            u.device_fingerprint
        FROM user_behavior_logs ubl
        JOIN users u ON u.id = ubl.user_id
        WHERE ubl.checkout_time IS NOT NULL AND ubl.page_load_time IS NOT NULL
    """
    df = pd.read_sql(text(query), db.connection())

    if len(df) < min_sessions:
        print(f"Only {len(df)} real sessions logged -- need at least {min_sessions} for a "
              f"meaningful scoring pass (Isolation Forest needs a real batch to compare against).")
        print("Run some checkouts through the API (or demo_5_benchmark.py) first, then re-run this.")
        return df.iloc[0:0]

    df["checkout_latency_ms"] = (df["checkout_time"] - df["page_load_time"]).dt.total_seconds() * 1000
    df["session_duration_ms"] = df["session_duration_ms"].fillna(df["checkout_latency_ms"])

    device_counts = pd.read_sql(
        text("SELECT device_fingerprint, COUNT(*) AS accounts_per_device FROM users GROUP BY device_fingerprint"),
        db.connection(),
    )
    df = df.merge(device_counts, on="device_fingerprint", how="left")
    df["accounts_per_device"] = df["accounts_per_device"].fillna(1)

    df["minute_bucket"] = df["checkout_time"].dt.floor("min")
    df["requests_per_ip_per_min"] = df.groupby(["ip_address", "minute_bucket"])["log_id"].transform("count")

    feature_cols = ["checkout_latency_ms", "session_duration_ms", "accounts_per_device", "requests_per_ip_per_min"]
    X = df[feature_cols]

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = IsolationForest(n_estimators=200, contamination=contamination, random_state=42)
    model.fit(X_scaled)
    raw_pred = model.predict(X_scaled)
    df["flagged"] = (raw_pred == -1).astype(int)
    df["anomaly_score"] = -model.decision_function(X_scaled)

    flaggable = df[(df["flagged"] == 1) & df["order_id"].notna()].copy()

    for _, row in flaggable.iterrows():
        db.execute(
            text("""
                INSERT INTO flagged_orders (id, order_id, anomaly_score, flagged_at, review_status)
                VALUES (gen_random_uuid(), :oid, :score, now(), 'pending')
                ON CONFLICT (order_id) DO UPDATE
                SET anomaly_score = EXCLUDED.anomaly_score, flagged_at = now(), review_status = 'pending'
            """),
            {"oid": row["order_id"], "score": float(row["anomaly_score"])},
        )
        db.execute(text("UPDATE orders SET status = 'flagged' WHERE id = :oid"), {"oid": row["order_id"]})
    db.commit()

    print("Bot/Scalper Detection -- real-data scoring pass")
    print("=" * 60)
    print(f"Sessions scored: {len(df)}")
    print(f"Flagged as anomalous: {int(df['flagged'].sum())}")
    print(f"  ...of which tied to a real (successful) order and written to FlaggedOrder: {len(flaggable)}")
    if len(flaggable):
        print("\nFlagged sessions:")
        print(flaggable[["order_id"] + feature_cols + ["anomaly_score"]].to_string(index=False))

    return flaggable


if __name__ == "__main__":
    train_and_evaluate()
