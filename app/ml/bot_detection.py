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
"""
import numpy as np
import pandas as pd
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


if __name__ == "__main__":
    train_and_evaluate()
