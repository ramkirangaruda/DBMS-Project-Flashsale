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

WHY THE REAL-DATA R^2 IS LOW (~0.08), AND WHY THAT IS NOT A BUG
---------------------------------------------------------------
There is a structural mismatch between what this project wants to predict
and what the UCI dataset can answer.

The system's actual question is flash-sale burst velocity: "how many units
will move in the first 60 seconds of a scarce, time-boxed sale event?" The
UCI Online Retail dataset contains no flash-sale events at all. It is a
year of ordinary invoice lines from a gift retailer -- steady wholesale and
retail reordering, aggregated to daily granularity at best. There is no
event to be fast during. So the real-data path predicts the nearest
answerable question instead (next-day units sold per SKU), and that
substitution, not the model, is what caps the score.

Two properties of the data make daily prediction especially hard:
  - ~51% of SKU-days in the panel have ZERO sales. The target is a
    spike-and-zero series, so a large share of the variance is arrival
    timing rather than demand level.
  - Per-SKU daily counts at this granularity are dominated by lumpy
    wholesale reorders, which are close to unpredictable from price and
    calendar features alone.

compare_framings() below quantifies this rather than asserting it. Measured
on this dataset (see docs/results.md for the captured run):
  - next-day units (the shipped framing):          R^2 = 0.082
  - next-7-day total units:                        R^2 = 0.270
  - next-week units (weekly panel):                R^2 = 0.259
  - same-day units (nowcast, not a forecast):      R^2 = 0.319
  - same-week units (nowcast, weekly):             R^2 = 0.484
  - the SAME model code on synthetic data:         R^2 = 0.941

Read those together and the diagnosis is unambiguous. The pipeline is
correct -- identical model code scores 0.941 when the target is actually
learnable from the features. Coarsening the horizon roughly triples R^2
(0.082 -> ~0.27), because aggregation averages out the day-to-day arrival
noise that dominates the daily target. And the nowcast rows show the
features do carry genuine signal (0.319 / 0.484); it is specifically the
step forward in time that this dataset does not support well.

Note that R^2 is NOT directly comparable across these rows -- each target
has its own variance denominator -- so the numbers indicate which questions
are answerable, not a leaderboard. The synthetic 0.941 in particular is a
ceiling produced by construction, not an achievement: that generator's
labels ARE a known formula of its features plus Gaussian noise, so a good
model must recover it. Quoting it as evidence of model quality would be
dishonest; its only legitimate use is the one made here -- as a control
proving the training/evaluation code works, isolating the low real-data
score as a data-framing limit.

The shipped default remains next-day units so the reported MAE stays in
directly interpretable units, with the better-posed horizons reported
alongside rather than silently swapped in.

Run with: venv/bin/python -m app.ml.demand_forecast
             (add --framings to also run the framing comparison above;
              it retrains several models and takes a few extra minutes)
"""
import json
import os
import threading

# numpy/pandas/scikit-learn are imported lazily, inside the functions that
# actually train a model, rather than up here at module load. That is what
# lets app/main.py import get_forecast_sample() -- and, transitively, this
# whole module -- in the Vercel deployment without those three packages
# needing to be in the serverless function's bundle at all: get_forecast_sample()
# never reaches the functions below it when IS_SERVERLESS, it serves
# forecast_sample.json instead. See the note on IS_SERVERLESS further down.
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
    import numpy as np
    import pandas as pd

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


def _build_daily_panel(path=DATA_PATH, top_n_skus=500, pre_period_days=7):
    """
    Loads the UCI "Online Retail" dataset and reframes it into the same
    purchase-velocity-style supervised learning problem the synthetic
    generator demonstrates: a per-SKU daily panel carrying price,
    discounting, item popularity, and calendar effects.

    Returns the panel WITHOUT a target column attached, so callers can hang
    different targets off the same features -- load_real_data() uses
    next-day units, compare_framings() also derives 7-day and weekly
    horizons from this same panel.

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

    Restricted to the `top_n_skus` best-selling SKUs so each item has
    enough trading history for a stable rolling pre-period feature and a
    well-defined "next day" -- a normal curation step for a per-SKU time
    series model, not a synthetic shortcut.

    Missing days are reindexed in with qty=0 rather than dropped, so "next
    day" and "trailing N days" mean actual calendar time. That is the honest
    construction, and it is also why ~51% of panel rows have a zero target:
    these SKUs genuinely do not sell every day. See the module docstring.
    """
    import pandas as pd

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
    panel = panel.rename(columns={"price": "base_price"})

    return panel


def load_real_data(path=DATA_PATH, top_n_skus=500, pre_period_days=7):
    """Shipped real-data framing: predict each SKU's units sold on the NEXT
    calendar day. See the module docstring for why this target scores low and
    which alternative horizons score better -- compare_framings() measures
    them."""
    panel = _build_daily_panel(path, top_n_skus, pre_period_days)
    panel["next_day_qty"] = panel.groupby("StockCode")["qty"].shift(-1)
    panel = panel.dropna(subset=["pre_period_demand", "next_day_qty"])
    return panel[REAL_FEATURE_COLS + [REAL_TARGET_COL]].reset_index(drop=True)


def _fit(force_synthetic=False):
    """Shared training step behind both train_and_evaluate() (prints a full
    report) and get_forecast_sample() (returns JSON for the live dashboard)."""
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error, r2_score

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


def _eval_framing(df, feature_cols, target_col):
    """Fit the SAME model config used everywhere else on an arbitrary
    feature/target pair. Used by compare_framings() so every row of that
    table differs only in what is being predicted, never in how."""
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error, r2_score

    d = df.dropna(subset=list(feature_cols) + [target_col])
    if len(d) < 100:
        return None
    X_train, X_test, y_train, y_test = train_test_split(
        d[feature_cols], d[target_col], test_size=0.2, random_state=42
    )
    model = GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
    model.fit(X_train, y_train)
    preds = model.predict(X_test)
    return {
        "r2": r2_score(y_test, preds),
        "mae": mean_absolute_error(y_test, preds),
        "n": len(d),
    }


def compare_framings(path=DATA_PATH):
    """
    Measures how much of the low real-data R^2 is the TARGET's fault rather
    than the model's, by re-asking the question at several horizons and
    granularities with identical model code.

    Included deliberately: two "nowcast" rows that predict the CURRENT
    period instead of a future one. Those are not forecasts and could never
    ship -- a nowcast needs today's sales to predict today's sales. They are
    diagnostics: if the features explain the present far better than the
    future, the gap is forecasting difficulty in the data, not weak features
    or a broken pipeline.

    R^2 is NOT comparable across rows -- each target has its own variance --
    so read this as which questions this dataset can answer, not a ranking.
    """
    if not os.path.exists(path):
        print(f"WARNING: {path} not found -- compare_framings() needs the real dataset.")
        return []

    print("Building the per-SKU daily panel (loads the xlsx once)...")
    panel = _build_daily_panel(path)
    zero_share = (panel["qty"] == 0).mean()

    rows = []

    panel["next_day_qty"] = panel.groupby("StockCode")["qty"].shift(-1)
    rows.append(("next-day units (SHIPPED framing)", "forecast",
                 _eval_framing(panel, REAL_FEATURE_COLS, "next_day_qty")))

    panel["next_7d_qty"] = panel.groupby("StockCode")["qty"].transform(
        lambda s: s.shift(-7).rolling(7, min_periods=7).sum())
    rows.append(("next-7-day total units", "forecast",
                 _eval_framing(panel, REAL_FEATURE_COLS, "next_7d_qty")))

    rows.append(("same-day units (NOWCAST, diagnostic)", "not a forecast",
                 _eval_framing(panel, REAL_FEATURE_COLS, "qty")))

    # Weekly panel -- coarser grain, rebuilt from the same daily rows.
    wk = panel.reset_index().rename(columns={"index": "day"})
    wk["week"] = wk["day"].dt.to_period("W").dt.start_time
    weekly = wk.groupby(["StockCode", "week"]).agg(
        qty=("qty", "sum"),
        base_price=("base_price", "mean"),
        discount_pct=("discount_pct", "mean"),
        category_popularity=("category_popularity", "first"),
    ).reset_index()
    weekly["pre_period_demand"] = weekly.groupby("StockCode")["qty"].transform(
        lambda s: s.shift(1).rolling(4, min_periods=1).sum())
    weekly["week_of_year"] = weekly["week"].dt.isocalendar().week.astype(int)
    weekly["next_week_qty"] = weekly.groupby("StockCode")["qty"].shift(-1)
    wfeat = ["base_price", "discount_pct", "category_popularity", "pre_period_demand", "week_of_year"]

    rows.append(("next-week units (weekly panel)", "forecast",
                 _eval_framing(weekly, wfeat, "next_week_qty")))
    rows.append(("same-week units (NOWCAST, diagnostic)", "not a forecast",
                 _eval_framing(weekly, wfeat, "qty")))

    syn = _fit(force_synthetic=True)
    rows.append(("SYNTHETIC generator (control)", "control",
                 {"r2": syn["r2"], "mae": syn["mae"], "n": len(syn["X_train"]) + len(syn["X_test"])}))

    print()
    print("Framing comparison -- same model code, different questions")
    print("=" * 88)
    print(f"~{zero_share:.0%} of SKU-days in the panel have ZERO sales, which is what makes")
    print("the daily target so hard: much of its variance is arrival timing, not demand level.")
    print()
    print(f"{'Framing':<40} {'Kind':<16} {'R^2':>8} {'MAE':>10} {'rows':>10}")
    print("-" * 88)
    for label, kind, res in rows:
        if res is None:
            print(f"{label:<40} {kind:<16} {'--':>8} {'--':>10} {'too few':>10}")
            continue
        print(f"{label:<40} {kind:<16} {res['r2']:>8.3f} {res['mae']:>10.2f} {res['n']:>10,}")
    print("-" * 88)
    print("R^2 is not comparable across rows (different variance denominators);")
    print("this shows which questions the data can answer, not a leaderboard.")
    print()
    print("Reading: the synthetic control scores high because its labels ARE a known")
    print("formula of its features plus noise -- that is a ceiling by construction, not")
    print("a result. Its only job here is to prove the training/eval code is sound, which")
    print("isolates the low real-data score as a property of the data and the horizon.")
    print("Coarsening the horizon roughly triples R^2; the nowcast rows show the features")
    print("do carry signal, so it is the step forward in time this dataset resists.")
    return rows


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

    # Same-run control: retrain the SAME model code on the synthetic
    # generator and print both R^2 values together. This is the cheapest
    # honest way to separate "the implementation is broken" from "this
    # dataset cannot answer this question", and it costs ~1s.
    if r["source_label"].startswith("UCI"):
        syn = _fit(force_synthetic=True)
        print()
        print("Sanity control -- SAME model code, synthetic data:")
        print("-" * 62)
        print(f"  real (UCI, {REAL_TARGET_COL:<16})  R^2 = {r['r2']:.3f}   MAE = {r['mae']:.2f}")
        print(f"  synthetic ({SYNTHETIC_TARGET_COL:<16})  R^2 = {syn['r2']:.3f}   MAE = {syn['mae']:.2f}")
        print()
        print("  The pipeline is not broken: identical code scores high when the target")
        print("  is genuinely learnable from the features. But the synthetic score is a")
        print("  ceiling BY CONSTRUCTION -- those labels are a known formula of those")
        print("  features plus Gaussian noise, so recovering it proves only that the")
        print("  training/evaluation path works. It is a control, not an achievement,")
        print("  and must not be quoted as this project's forecasting accuracy.")
        print()
        print("  The low real score is a data-framing mismatch: UCI Online Retail has no")
        print("  flash-sale events, so 'first-60-second burst velocity' is not answerable")
        print("  from it and next-day units is the nearest substitute. ~51% of SKU-days")
        print("  have zero sales, so much of that target is arrival timing, not demand")
        print("  level. Run with --framings to measure horizons that suit the data better")
        print("  (next-7-day and next-week both roughly triple R^2).")

    print()
    print("Use case: predicted next-period demand feeds the safety-stock buffer --")
    print("a SKU predicted at high volume should get pessimistic locking")
    print("(correctness under heavy contention); a SKU predicted at low volume")
    print("can safely use OCC (lower overhead, low contention).")

    return model


_dashboard_cache = None
_dashboard_lock = threading.Lock()

# Same flag app/queue.py checks -- Vercel sets VERCEL=1 in every function's
# environment. There is no data/online_retail.xlsx in that deployment (it's
# a ~23MB file, gitignored, never uploaded to Vercel -- see the README), and
# training even the synthetic fallback needs scikit-learn/pandas/numpy,
# which would otherwise have to ship inside the serverless function's
# package purely to explain a demo chart. So on Vercel this serves a
# precomputed sample instead of training anything -- see
# STATIC_SAMPLE_PATH below.
IS_SERVERLESS = bool(os.getenv("VERCEL"))

STATIC_SAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forecast_sample.json")


def _static_forecast_sample(n):
    """The exact output of get_forecast_sample(n=10) against the real UCI
    dataset, captured once and committed -- see the README's Section 10.1
    writeup for the same numbers (MAE 21.84, R^2 0.082). Regenerate it with
    `python -m app.ml.demand_forecast --write-static-sample` after retraining."""
    with open(STATIC_SAMPLE_PATH) as f:
        cached = json.load(f)
    cached["samples"] = cached["samples"][:n]
    return cached


def get_forecast_sample(n=10):
    """Trains once (cached for the life of the process) and returns a small
    JSON-friendly sample of predicted-vs-actual next-day demand, for the
    live dashboard's chart. Not re-trained per request -- this is a demo
    aid, not a serving pipeline.

    Guarded by a lock so the startup warm-up thread and an early dashboard
    request can't both see an empty cache and each kick off their own
    multi-minute training run at the same time."""
    if IS_SERVERLESS:
        return _static_forecast_sample(n)

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
    import sys

    if "--write-static-sample" in sys.argv:
        # Regenerates app/ml/forecast_sample.json (see _static_forecast_sample
        # above) from a fresh training run against whatever's at DATA_PATH --
        # run this after retraining if you want the Vercel deployment's
        # /demand-forecast chart to reflect it. IS_SERVERLESS is False for a
        # local run, so this trains rather than reading the static file back.
        sample = get_forecast_sample(n=10)
        with open(STATIC_SAMPLE_PATH, "w") as f:
            json.dump(sample, f, indent=2)
        print(f"Wrote {STATIC_SAMPLE_PATH}")
        print(json.dumps(sample, indent=2))
    else:
        train_and_evaluate()
        if "--framings" in sys.argv:
            print()
            compare_framings()
