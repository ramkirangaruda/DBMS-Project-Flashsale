"""
Section 10.1 -- Demand Forecasting

Trains a gradient-boosting regressor to predict purchase velocity from
historical demand features. This is what feeds the safety-stock buffer
decision mentioned in the design doc: a SKU predicted at high volume
should get pessimistic locking (correctness under heavy contention); a
SKU predicted at low volume can safely use OCC (lower overhead, low
contention).

DATA: load_real_data() trains on the UCI "Online Retail" dataset --
~541k line items from a UK-based online gift retailer, Dec 2010-Dec 2011
(https://archive.ics.uci.edu/ml/machine-learning-databases/00352/Online%20Retail.xlsx).
Download it to data/online_retail.xlsx (not committed -- see README) and
this is the default path train_and_evaluate() takes.

generate_synthetic_fallback_data() is kept only so this script still runs
for someone without the xlsx file -- it's a labeled synthetic generator
that mimics realistic flash-sale demand patterns, NOT real data. If
data/online_retail.xlsx is missing, train_and_evaluate() falls back to it
automatically and prints a warning; it is not the default path.

Run with: venv/bin/python -m app.ml.demand_forecast
"""
import os
import threading
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, r2_score

DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "online_retail.xlsx"
)

REAL_FEATURE_COLS = ["base_price", "discount_pct", "category_popularity", "day_of_week", "pre_period_demand", "is_weekend"]
REAL_TARGET_COL = "next_day_qty"

SYNTHETIC_FEATURE_COLS = ["base_price", "discount_pct", "category_popularity", "day_of_week", "wishlist_count_pre_sale", "is_weekend"]
SYNTHETIC_TARGET_COL = "orders_first_60s"


def generate_synthetic_fallback_data(n=2000, seed=42):
    """FALLBACK / DEMO-ONLY generator. Used only when data/online_retail.xlsx
    isn't present, so this script still runs end to end for someone without
    the dataset. Produces a labeled synthetic dataset that mimics realistic
    flash-sale demand patterns (price elasticity, weekday effects, category
    popularity, promotional buzz) -- but it is NOT real data. See
    load_real_data() for the actual Section 10.1 model."""
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


def load_real_data(path=DATA_PATH, top_n_skus=500, pre_period_days=7):
    """
    Loads the UCI "Online Retail" dataset and reframes it into the same
    purchase-velocity-style supervised learning problem the synthetic
    generator demonstrates: predict next-day purchase volume per SKU from
    price, discounting, item popularity, and calendar effects.

    Cleaning:
      - drop cancelled orders (InvoiceNo starting with 'C')
      - drop rows with missing CustomerID
      - drop non-positive Quantity / UnitPrice

    Feature engineering (real analogues of the synthetic feature set --
    see the README for the full name-by-name mapping):
      - base_price          : mean UnitPrice for that SKU on that day
      - discount_pct        : how far today's price sits below this SKU's
                               own median price, clipped at 0 -- there's
                               no explicit discount field in the raw data,
                               so a price dip below the item's own normal
                               price is the closest honest proxy
      - category_popularity : SKU's total-units-sold rank across the whole
                               dataset, bucketed 1 (niche) - 5 (bestseller)
      - day_of_week / is_weekend : from the invoice date
      - pre_period_demand   : trailing `pre_period_days`-day unit sales for
                               that SKU, EXCLUDING today -- the real
                               analogue of wishlist_count_pre_sale (a
                               pre-event signal that predicts what happens
                               next, just measured in actual past sales
                               instead of wishlist adds)

    Target: next_day_qty, this SKU's total units sold on the following
    calendar day.

    Restricted to the `top_n_skus` best-selling SKUs so each item has
    enough trading history for a stable rolling pre-period feature and a
    well-defined "next day" -- a normal curation step for a per-SKU time
    series model, not a synthetic shortcut.
    """
    df = pd.read_excel(path)

    df = df[~df["InvoiceNo"].astype(str).str.startswith("C")]
    df = df.dropna(subset=["CustomerID"])
    df = df[(df["Quantity"] > 0) & (df["UnitPrice"] > 0)]

    df["day"] = df["InvoiceDate"].dt.floor("D")

    top_skus = df.groupby("StockCode")["Quantity"].sum().nlargest(top_n_skus).index
    df = df[df["StockCode"].isin(top_skus)]

    daily = df.groupby(["StockCode", "day"]).agg(
        qty=("Quantity", "sum"),
        price=("UnitPrice", "mean"),
    ).reset_index()

    total_by_sku = df.groupby("StockCode")["Quantity"].sum()
    popularity_rank = pd.qcut(total_by_sku, 5, labels=False, duplicates="drop") + 1
    daily["category_popularity"] = daily["StockCode"].map(popularity_rank)

    median_price_by_sku = df.groupby("StockCode")["UnitPrice"].median()
    daily["median_price"] = daily["StockCode"].map(median_price_by_sku)
    daily["discount_pct"] = ((daily["median_price"] - daily["price"]) / daily["median_price"]).clip(lower=0)

    # Build a continuous per-SKU daily panel (filling no-sale days with 0
    # quantity) so "next calendar day" and "trailing N days" are both
    # well-defined -- not just "the next day this SKU happened to sell".
    frames = []
    for sku, g in daily.groupby("StockCode"):
        g = g.set_index("day").sort_index()
        full_range = pd.date_range(g.index.min(), g.index.max(), freq="D")
        g = g.reindex(full_range)
        g["StockCode"] = sku
        g["qty"] = g["qty"].fillna(0)
        g[["price", "category_popularity", "median_price"]] = g[["price", "category_popularity", "median_price"]].ffill().bfill()
        g["discount_pct"] = g["discount_pct"].fillna(0)
        frames.append(g)
    panel = pd.concat(frames)

    panel["day_of_week"] = panel.index.dayofweek
    panel["is_weekend"] = (panel["day_of_week"] >= 5).astype(int)

    panel["pre_period_demand"] = (
        panel.groupby("StockCode")["qty"]
        .transform(lambda s: s.shift(1).rolling(pre_period_days, min_periods=1).sum())
    )
    panel["next_day_qty"] = panel.groupby("StockCode")["qty"].shift(-1)

    panel = panel.dropna(subset=["pre_period_demand", "next_day_qty"])
    panel = panel.rename(columns={"price": "base_price"})

    return panel[REAL_FEATURE_COLS + [REAL_TARGET_COL]].reset_index(drop=True)


def _fit(force_synthetic=False):
    """Shared training step behind both train_and_evaluate() (prints a full
    report) and get_forecast_sample() (returns JSON for the live dashboard)."""
    use_real = (not force_synthetic) and os.path.exists(DATA_PATH)

    if use_real:
        df = load_real_data()
        feature_cols, target_col = REAL_FEATURE_COLS, REAL_TARGET_COL
        source_label = "UCI Online Retail dataset (real data)"
    else:
        df = generate_synthetic_fallback_data()
        feature_cols, target_col = SYNTHETIC_FEATURE_COLS, SYNTHETIC_TARGET_COL
        source_label = "synthetic demo generator (NOT real data)"

    X = df[feature_cols]
    y = df[target_col]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    return {
        "model": model, "feature_cols": feature_cols, "target_col": target_col,
        "source_label": source_label, "X_train": X_train, "X_test": X_test,
        "y_train": y_train, "y_test": y_test, "preds": preds,
        "mae": mean_absolute_error(y_test, preds), "r2": r2_score(y_test, preds),
    }


def train_and_evaluate(force_synthetic=False):
    if (not force_synthetic) and os.path.exists(DATA_PATH):
        print(f"Loading real data from {DATA_PATH} ...")
    else:
        if not force_synthetic:
            print(f"WARNING: {DATA_PATH} not found -- falling back to the SYNTHETIC")
            print("demo generator (NOT real data). Download the real dataset per the")
            print("README to train on actual data instead:")
            print("  https://archive.ics.uci.edu/ml/machine-learning-databases/00352/Online%20Retail.xlsx\n")

    r = _fit(force_synthetic)
    model, feature_cols, target_col = r["model"], r["feature_cols"], r["target_col"]
    preds, y_test = r["preds"], r["y_test"]

    print("Demand Forecasting Model")
    print("=" * 50)
    print(f"Data source: {r['source_label']}")
    print(f"Training rows: {len(r['X_train']):,}   Test rows: {len(r['X_test']):,}")
    print(f"Mean Absolute Error: {r['mae']:.2f} units ({target_col})")
    print(f"R^2 score: {r['r2']:.3f}")
    print()
    print("Feature importances:")
    for name, imp in sorted(zip(feature_cols, model.feature_importances_), key=lambda x: -x[1]):
        print(f"  {name:28} {imp:.3f}")

    print()
    print("Example predictions vs actual (first 5 test rows):")
    for i in range(5):
        print(f"  predicted={preds[i]:.1f}   actual={y_test.values[i]:.1f}")

    print()
    print("Use case: predicted next-period demand feeds the safety-stock buffer --")
    print("a SKU predicted at high volume should get pessimistic locking")
    print("(correctness under heavy contention); a SKU predicted at low volume")
    print("can safely use OCC (lower overhead, low contention).")

    return model


_dashboard_cache = None
_dashboard_lock = threading.Lock()


def get_forecast_sample(n=10):
    """Trains once (cached for the life of the process) and returns a small
    JSON-friendly sample of predicted-vs-actual next-day demand, for the
    live dashboard's chart. Not re-trained per request -- this is a demo
    aid, not a serving pipeline.

    Guarded by a lock so the startup warm-up thread and an early dashboard
    request can't both see an empty cache and each kick off their own
    multi-minute training run at the same time."""
    global _dashboard_cache
    if _dashboard_cache is None:
        with _dashboard_lock:
            if _dashboard_cache is None:
                _dashboard_cache = _fit()
    r = _dashboard_cache
    preds, y_test = r["preds"], r["y_test"]
    n = min(n, len(y_test))
    return {
        "source": r["source_label"],
        "mae": round(float(r["mae"]), 2),
        "r2": round(float(r["r2"]), 3),
        "samples": [
            {"label": f"SKU sample {i+1}", "predicted": round(float(preds[i]), 1), "actual": round(float(y_test.values[i]), 1)}
            for i in range(n)
        ],
    }


if __name__ == "__main__":
    train_and_evaluate()
