"""
Section 10.1 -- Demand Forecasting

Trains a gradient-boosting regressor to predict purchase velocity (orders
in the first N seconds of a sale) from historical sale features. This is
what feeds the safety-stock buffer decision mentioned in the design doc.

NOTE ON DATA: this script generates a *labeled synthetic* dataset that
mimics realistic flash-sale demand patterns (price elasticity, weekday
effects, category popularity, promotional buzz). It's built so you can
swap in a real dataset (e.g. Kaggle's "Online Retail Dataset" or "M5
Forecasting") by replacing `generate_training_data()` with a loader that
returns a DataFrame with the same column names -- the model code doesn't
change. Being upfront about this in your report is a good thing to call
out explicitly: it shows you understand the difference between a
demonstration dataset and a production one.

Run with: venv/bin/python -m app.ml.demand_forecast
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score


def generate_training_data(n=2000, seed=42):
    rng = np.random.default_rng(seed)

    base_price = rng.uniform(20, 300, n)
    discount_pct = rng.uniform(0.1, 0.7, n)
    category_popularity = rng.integers(1, 6, n)          # 1 (niche) - 5 (viral category)
    day_of_week = rng.integers(0, 7, n)                    # 0=Mon ... 6=Sun
    wishlist_count_pre_sale = rng.poisson(lam=category_popularity * 40, size=n)
    is_weekend = (day_of_week >= 5).astype(int)

    # Ground truth generative model: higher discount, higher popularity, more
    # wishlists, and weekends all push purchase velocity up, with noise.
    velocity = (
        5
        + discount_pct * 40
        + category_popularity * 8
        + wishlist_count_pre_sale * 0.15
        + is_weekend * 6
        + rng.normal(0, 5, n)
    )
    velocity = np.clip(velocity, 0, None)

    return pd.DataFrame({
        "base_price": base_price,
        "discount_pct": discount_pct,
        "category_popularity": category_popularity,
        "day_of_week": day_of_week,
        "wishlist_count_pre_sale": wishlist_count_pre_sale,
        "is_weekend": is_weekend,
        "orders_first_60s": velocity,
    })


def train_and_evaluate():
    df = generate_training_data()
    feature_cols = ["base_price", "discount_pct", "category_popularity", "day_of_week", "wishlist_count_pre_sale", "is_weekend"]
    X = df[feature_cols]
    y = df["orders_first_60s"]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    mae = mean_absolute_error(y_test, preds)
    r2 = r2_score(y_test, preds)

    print("Demand Forecasting Model")
    print("=" * 50)
    print(f"Mean Absolute Error: {mae:.2f} orders in first 60s")
    print(f"R^2 score: {r2:.3f}")
    print()
    print("Feature importances:")
    for name, imp in sorted(zip(feature_cols, model.feature_importances_), key=lambda x: -x[1]):
        print(f"  {name:28} {imp:.3f}")

    print()
    print("Example predictions vs actual (first 5 test rows):")
    for i in range(5):
        print(f"  predicted={preds[i]:.1f}   actual={y_test.values[i]:.1f}")

    print()
    print("Use case: predicted orders_first_60s feeds the safety-stock buffer --")
    print("a SKU predicted at 80 orders/60s should get pessimistic locking")
    print("(correctness under heavy contention); a SKU predicted at 3 orders/60s")
    print("can safely use OCC (lower overhead, low contention).")

    return model


if __name__ == "__main__":
    train_and_evaluate()
