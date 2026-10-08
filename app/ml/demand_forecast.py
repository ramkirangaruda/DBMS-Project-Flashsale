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

HOW THE SHIPPED MODEL IS TRAINED AND EVALUATED
----------------------------------------------
  - Target: each SKU's units sold on the NEXT calendar day, winsorized at the
    99.5th percentile of the daily panel (360 units). Raw daily quantities are
    heavy-tailed (median 0, max 80,995) and a few wholesale orders would
    otherwise dominate squared error; the cap is applied to features and
    target and every metric is on the capped target.
  - Features: price, discount proxy, popularity bucket, calendar, plus per-SKU
    lag-1/lag-7 and rolling 7/14/28-day mean demand and sale frequency. All
    use shift(1) first, so they only see strictly earlier days.
  - Split: TIME-BASED -- train on the first 80% of the calendar, test on the
    last 20%, boundary day purged. (A random split on per-SKU time series
    lets rolling features straddle the split and flatters the score.)
  - Baselines on the same test rows: "yesterday" and "trailing-28-day mean".
  - Result (see app/ml/artifacts/demand_metrics.json): MAE 21.48 vs 25.11
    for the best baseline, R^2 0.178 vs 0.155. The model beats both baselines,
    but modestly -- see below for why that is expected.

WHY R^2 STAYS LOW, AND WHY THAT IS NOT A BUG
--------------------------------------------
The system's real question is flash-sale burst velocity ("units in the first
60 seconds of a scarce, time-boxed event"). The UCI Online Retail dataset has
no flash-sale events: it is a year of ordinary invoice lines, ~51% of SKU-days
have ZERO sales, and per-SKU daily counts are dominated by lumpy wholesale
reorders. The nearest answerable question is next-day units, and that
substitution, not the model, caps the score. The same code scores R^2 0.941 on
the synthetic control, whose labels ARE a known formula of its features plus
noise -- a ceiling by construction that only proves the training/evaluation
path works and must never be quoted as forecasting accuracy.

A negative finding worth keeping: on a 7-day horizon (winsorized, time-based
split) a plain trailing-28-day mean (R^2 0.505) BEAT the gradient-boosting
model (R^2 0.272), so the model is shipped only for the next-day question
where it does beat the baselines.

compare_framings() below is a LEGACY diagnostic: it still uses the original
six features and a random split, so its numbers (next-day 0.082, next-7-day
0.270, ...) are comparable to each other but are NOT comparable to the
time-based result above.

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

# Original six features -- still what compare_framings() (a legacy diagnostic,
# random split) uses, so its numbers stay comparable to what was reported.
LEGACY_FEATURE_COLS = ["base_price", "discount_pct", "category_popularity", "day_of_week", "pre_period_demand", "is_weekend"]
# Shipped model: the legacy six plus per-SKU lag / rolling-demand features.
REAL_FEATURE_COLS = LEGACY_FEATURE_COLS + [
    "lag1", "lag7", "roll7", "roll14", "roll28", "roll7_nonzero", "roll28_nonzero", "month",
]
REAL_TARGET_COL = "next_day_qty"
# Daily quantities are heavy-tailed (median 0, max 80,995): a handful of
# wholesale orders dominate squared error. Quantities are winsorized at this
# percentile of the daily panel, in features AND target, and every reported
# metric is on the winsorized target -- stated, not hidden.
WINSOR_QUANTILE = 0.995
TEST_FRACTION_OF_TIME = 0.2   # last 20% of the calendar is the held-out test period

ARTIFACT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")

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
    """Shipped real-data framing: predict each SKU's winsorized units sold on
    the NEXT calendar day, from lagged / rolling per-SKU demand plus price and
    calendar features. Returns the feature columns, the target, and `day`
    (kept for the time-based split, never used as a feature).

    All lag/rolling features use shift(1) first, so they only see strictly
    earlier days -- no row's features contain its own or any future target."""
    import pandas as pd

    panel = _build_daily_panel(path, top_n_skus, pre_period_days)
    panel.index.name = "day"
    p = panel.reset_index().sort_values(["StockCode", "day"])

    cap = float(p["qty"].quantile(WINSOR_QUANTILE))
    p["qty"] = p["qty"].clip(upper=cap)
    p["pre_period_demand"] = p.groupby("StockCode")["qty"].transform(
        lambda s: s.shift(1).rolling(pre_period_days, min_periods=1).sum())

    g = p.groupby("StockCode")["qty"]
    p["lag1"] = g.shift(1)
    p["lag7"] = g.shift(7)
    for w in (7, 14, 28):
        p[f"roll{w}"] = g.transform(lambda s, w=w: s.shift(1).rolling(w, min_periods=1).mean())
    p["roll7_nonzero"] = g.transform(lambda s: (s.shift(1) > 0).rolling(7, min_periods=1).mean())
    p["roll28_nonzero"] = g.transform(lambda s: (s.shift(1) > 0).rolling(28, min_periods=1).mean())
    p["month"] = p["day"].dt.month
    p[REAL_TARGET_COL] = g.shift(-1)

    p = p.dropna(subset=["pre_period_demand", "lag7", REAL_TARGET_COL])
    out = p[REAL_FEATURE_COLS + [REAL_TARGET_COL, "day"]].reset_index(drop=True)
    out.attrs["winsor_cap"] = cap
    return out


def _fit(force_synthetic=False, save=False):
    """Shared training step behind train_and_evaluate() (prints a full report)
    and get_forecast_sample() (returns JSON for the live dashboard).

    Real data: TIME-BASED split -- train on the first 80% of the calendar, test
    on the final 20%, with the one-day boundary purged. A random split on a
    per-SKU time series lets rolling features leak across the split and
    flatters the score; this measures what forecasting actually requires.
    Also scores two naive baselines (yesterday, trailing-28-day mean) on the
    same test rows: a model that cannot beat them is not worth shipping."""
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_absolute_error, r2_score

    use_real = (not force_synthetic) and os.path.exists(DATA_PATH)
    baselines = None
    split_info = None

    if use_real:
        import pandas as pd
        df = load_real_data()
        feature_cols, target_col = REAL_FEATURE_COLS, REAL_TARGET_COL
        source_label = "UCI Online Retail dataset (real data)"
        cut = df["day"].quantile(1 - TEST_FRACTION_OF_TIME)
        train_df = df[df["day"] <= cut - pd.Timedelta(days=1)]
        test_df = df[df["day"] > cut]
        X_train, y_train = train_df[feature_cols], train_df[target_col]
        X_test, y_test = test_df[feature_cols], test_df[target_col]
        baselines = {}
        for name, col in (("naive_yesterday", "lag1"), ("naive_trailing_28d_mean", "roll28")):
            bp = test_df[col].to_numpy()
            baselines[name] = {
                "mae": round(float(mean_absolute_error(y_test, bp)), 2),
                "r2": round(float(r2_score(y_test, bp)), 3),
            }
        split_info = {
            "method": "time-based (train on earlier calendar days, test on the last 20%)",
            "test_starts": str(cut.date()),
            "winsor_cap_units": df.attrs.get("winsor_cap"),
        }
        model = GradientBoostingRegressor(
            n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8, random_state=42)
    else:
        df = generate_synthetic_fallback_data()
        feature_cols, target_col = SYNTHETIC_FEATURE_COLS, SYNTHETIC_TARGET_COL
        source_label = "synthetic demo generator (NOT real data)"
        X_train, X_test, y_train, y_test = train_test_split(
            df[feature_cols], df[target_col], test_size=0.2, random_state=42)
        model = GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)

    model.fit(X_train, y_train)
    preds = np.clip(model.predict(X_test), 0, None)
    result = {
        "model": model, "feature_cols": feature_cols, "target_col": target_col,
        "source_label": source_label, "X_train": X_train, "X_test": X_test,
        "y_train": y_train, "y_test": y_test, "preds": preds,
        "mae": mean_absolute_error(y_test, preds), "r2": r2_score(y_test, preds),
        "baselines": baselines, "split": split_info,
    }
    if save and use_real:
        _save_artifacts(result)
    return result


def _save_artifacts(r):
    """Persist the trained model (gitignored) and its metrics (committed)."""
    import joblib
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    joblib.dump({"model": r["model"], "feature_cols": r["feature_cols"]},
                os.path.join(ARTIFACT_DIR, "demand_model.joblib"))
    metrics = {
        "source": r["source_label"], "target": r["target_col"],
        "train_rows": int(len(r["X_train"])), "test_rows": int(len(r["X_test"])),
        "mae": round(float(r["mae"]), 2), "r2": round(float(r["r2"]), 3),
        "baselines": r["baselines"], "split": r["split"],
        "features": r["feature_cols"],
        "feature_importance": {
            n: round(float(i), 4)
            for n, i in sorted(zip(r["feature_cols"], r["model"].feature_importances_), key=lambda x: -x[1])
        },
    }
    with open(os.path.join(ARTIFACT_DIR, "demand_metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)


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
                 _eval_framing(panel, LEGACY_FEATURE_COLS, "next_day_qty")))

    panel["next_7d_qty"] = panel.groupby("StockCode")["qty"].transform(
        lambda s: s.shift(-7).rolling(7, min_periods=7).sum())
    rows.append(("next-7-day total units", "forecast",
                 _eval_framing(panel, LEGACY_FEATURE_COLS, "next_7d_qty")))

    rows.append(("same-day units (NOWCAST, diagnostic)", "not a forecast",
                 _eval_framing(panel, LEGACY_FEATURE_COLS, "qty")))

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

    r = _fit(force_synthetic, save=True)
    model, feature_cols, target_col = r["model"], r["feature_cols"], r["target_col"]
    preds, y_test = r["preds"], r["y_test"]

    print("Demand Forecasting Model")
    print("=" * 50)
    print(f"Data source: {r['source_label']}")
    print(f"Training rows: {len(r['X_train']):,}   Test rows: {len(r['X_test']):,}")
    print(f"Mean Absolute Error: {r['mae']:.2f} units ({target_col})")
    print(f"R^2 score: {r['r2']:.3f}")
    if r["baselines"]:
        print(f"Split: {r['split']['method']}; test period starts {r['split']['test_starts']}")
        print(f"Target winsorized at {r['split']['winsor_cap_units']:.0f} units/day (99.5th pct)")
        print("Baselines on the SAME test rows (the model must beat these):")
        for name, b in r["baselines"].items():
            verdict = "model beats it" if r["mae"] < b["mae"] else "MODEL DOES NOT BEAT IT"
            print(f"  {name:26} MAE {b['mae']:6.2f}   R^2 {b['r2']:6.3f}   -> {verdict}")
        print(f"Saved model + metrics to {ARTIFACT_DIR}")
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
    import numpy as np
    if _dashboard_cache is None:
        with _dashboard_lock:
            if _dashboard_cache is None:
                _dashboard_cache = _fit()
    r = _dashboard_cache
    preds, y_test = r["preds"], r["y_test"]
    n = min(n, len(y_test))
    # Evenly spaced test rows, not just the first n (which would all be one SKU).
    idx = [int(i) for i in np.linspace(0, len(y_test) - 1, n)] if n else []
    out = {
        "source": r["source_label"],
        "mae": round(float(r["mae"]), 2),
        "r2": round(float(r["r2"]), 3),
        "samples": [
            {"label": f"SKU sample {k+1}", "predicted": round(float(preds[i]), 1), "actual": round(float(y_test.values[i]), 1)}
            for k, i in enumerate(idx)
        ],
    }
    if r.get("baselines"):
        out["baselines"] = r["baselines"]
        out["split"] = r["split"]
    return out


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
