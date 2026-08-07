"""
Collect the evidence for results/queue_notes.md: what an arm of the
raw-vs-queued comparison actually did.

    python -m scripts.queue_comparison --label raw
    python -m scripts.queue_comparison --label queued
    python -m scripts.queue_comparison --report

Each collection writes results/queue_comparison_{label}.json; --report diffs
the two and prints the table that goes into the write-up.

WHY THE SERVER IS RESTARTED BETWEEN ARMS
    app/admission.py reads ADMISSION_ENFORCED once at import, so switching the
    door on and off means a restart -- and a restart zeroes every Prometheus
    counter. That is convenient rather than annoying: after a restart the
    counter values ARE the arm's totals, with no window arithmetic and no
    risk of a neighbouring run bleeding in. Nothing else in the system
    increments the checkout counters.

WHAT IS COLLECTED
    * counters and histograms straight off /metrics (exact totals, not rates)
    * peak checkout_in_flight from Prometheus, which stores the samples over
      time that /metrics only shows instantaneously
    * pessimistic lock-wait times from Jaeger -- the real
      `SELECT ... FOR UPDATE` span durations of the run's own traffic, which
      is the only direct measurement of the thing the queue is supposed to
      reduce
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

API = os.getenv("CHAOS_BASE_URL", "http://127.0.0.1:8010")
JAEGER = os.getenv("JAEGER_URL", "http://127.0.0.1:16686")
PROMETHEUS = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "results")

STRATEGIES = ["pessimistic", "optimistic", "redis"]


# --------------------------------------------------------------------------
# /metrics
# --------------------------------------------------------------------------
def metrics_urls():
    """
    Where to read /metrics from.

    Defaults to the single host process. Set API_METRICS_URLS to a
    comma-separated list to read a scaled deployment -- the three replicas
    are published on 8011/8012/8013 for exactly this. Their counters are
    per-process and are summed below; reading through nginx would sample an
    arbitrary replica.
    """
    raw = os.getenv("API_METRICS_URLS")
    if raw:
        return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    return [API]


def scrape():
    """
    Merge /metrics across every instance.

    Counters and histogram buckets are additive across processes, so summing
    them reconstructs the fleet total. Gauges are NOT -- `checkout_in_flight`
    summed across replicas is the fleet's concurrent count, which is what we
    want, but a gauge like `current_queue_depth` is the same shared Redis
    value observed three times and must not be tripled. Gauges that describe
    shared state are therefore taken as a max rather than a sum.
    """
    from prometheus_client.parser import text_string_to_metric_families

    # Shared-state gauges: every replica reports the same Redis-backed number.
    SHARED_GAUGES = {"current_queue_depth", "flashsale_sale_available",
                     "flashsale_sale_divergence"}

    merged = {}
    for url in metrics_urls():
        try:
            text = httpx.get(f"{url}/metrics", timeout=20.0).text
        except Exception as exc:                               # noqa: BLE001
            print(f"  warning: could not scrape {url} ({type(exc).__name__})")
            continue
        for fam in text_string_to_metric_families(text):
            for s in fam.samples:
                key = (s.name, tuple(sorted(s.labels.items())))
                if s.name in SHARED_GAUGES:
                    prev = merged.get(key, (dict(s.labels), 0.0))[1]
                    merged[key] = (dict(s.labels), max(prev, s.value))
                else:
                    prev = merged.get(key, (dict(s.labels), 0.0))[1]
                    merged[key] = (dict(s.labels), prev + s.value)

    out = {}
    for (name, _), (labels, value) in merged.items():
        out.setdefault(name, []).append((labels, value))
    return out


def total(samples, name, **match):
    """Sum a counter, optionally filtered by labels."""
    acc = 0.0
    for labels, value in samples.get(name, []):
        if all(labels.get(k) == v for k, v in match.items()):
            acc += value
    return acc


def hist_quantile(samples, name, q, **match):
    """
    Quantile straight from the cumulative buckets, the same interpolation
    Prometheus's histogram_quantile uses.

    Computed over the whole arm rather than a rate() window on purpose: the
    arm is the unit of comparison, and a window would either clip it or drag
    in the idle time around it.
    """
    buckets = []
    for labels, value in samples.get(f"{name}_bucket", []):
        if all(labels.get(k) == v for k, v in match.items()):
            buckets.append((float(labels["le"]), value))
    if not buckets:
        return None
    buckets.sort()
    count = buckets[-1][1]
    if count <= 0:
        return None

    target = q * count
    prev_le, prev_count = 0.0, 0.0
    for le, cum in buckets:
        if cum >= target:
            if le == float("inf"):
                return prev_le
            span = cum - prev_count
            if span <= 0:
                return le
            return prev_le + (le - prev_le) * (target - prev_count) / span
        prev_le, prev_count = le, cum
    return buckets[-1][0]


# --------------------------------------------------------------------------
# Prometheus (for values that only exist over time)
# --------------------------------------------------------------------------
def prom(query):
    try:
        r = httpx.get(f"{PROMETHEUS}/api/v1/query", params={"query": query}, timeout=20.0)
        return r.json().get("data", {}).get("result", [])
    except Exception:                                          # noqa: BLE001
        return []


# Restricts Prometheus queries to one deployment. Set to
# '{job="flashsale-api-replicas"}' for the scaled stack so a stale
# single-instance series cannot leak into the numbers.
PROM_SELECTOR = os.getenv("PROM_SELECTOR", "")


def _sel(extra=""):
    inner = PROM_SELECTOR.strip("{}")
    parts = [p for p in (inner, extra) if p]
    return "{" + ",".join(parts) + "}" if parts else ""


def peak_in_flight(window="10m"):
    """
    Highest concurrent checkout count Prometheus SAW.

    A floor, not the true peak: scraping is every 2s, so a spike that opens
    and closes between scrapes is invisible here. chaos_verification.py's
    max_checkout_concurrency measures the same quantity exactly, from the
    client side, and is the number to trust when the two disagree.

    Summed across replicas -- concurrency is a fleet property, and the
    per-replica split is reported separately so an unbalanced load balancer
    would be visible rather than hidden by the sum.
    """
    out = {"per_strategy": {}, "total": None, "per_instance": {}}
    for s in STRATEGIES:
        res = prom(f'max_over_time(sum(checkout_in_flight{_sel(f'strategy="{s}"')})[{window}:2s])')
        if res:
            out["per_strategy"][s] = float(res[0]["value"][1])
    res = prom(f'max_over_time(sum(checkout_in_flight{_sel()})[{window}:2s])')
    if res:
        out["total"] = float(res[0]["value"][1])
    for r in prom(f'max_over_time(sum by (instance) (checkout_in_flight{_sel()})[{window}:2s])'):
        out["per_instance"][r["metric"].get("instance", "?")] = float(r["value"][1])
    return out


def scrape_health(window="10m"):
    """
    Did Prometheus manage to scrape during the run?

    The single-instance raw arm had a hole here -- the saturated process
    could not answer /metrics while the herd was hitting it, so the gauge
    series went blank exactly where the peak was. Reported explicitly now
    rather than inferred from a missing number.
    """
    out = {}
    for r in prom(f'avg_over_time(up{_sel()}[{window}])'):
        out[r["metric"].get("instance", "?")] = round(float(r["value"][1]), 4)
    misses = {}
    for r in prom(f'count_over_time((up{_sel()} == 0)[{window}:2s])'):
        misses[r["metric"].get("instance", "?")] = float(r["value"][1])
    return {"scrape_success_ratio": out, "failed_scrape_samples": misses}


# --------------------------------------------------------------------------
# Jaeger: real lock-wait durations from this arm's own traffic
# --------------------------------------------------------------------------
def _tags(span):
    return {t["key"]: t.get("value") for t in span.get("tags", [])}


def lock_waits(lookback_s=600, limit=1500):
    """
    Durations of every `SELECT ... FOR UPDATE` span in the window.

    This is the direct measurement of pessimistic lock contention: the
    statement blocks inside PostgreSQL until the row lock is granted, so the
    span's width IS the wait. No proxy, no inference from latency.
    """
    end_us = int(time.time() * 1_000_000)
    start_us = end_us - lookback_s * 1_000_000
    try:
        r = httpx.get(f"{JAEGER}/api/traces", timeout=90.0, params={
            "service": "flashsale-engine",
            "operation": "POST /checkout/pessimistic",
            "start": start_us, "end": end_us, "limit": limit,
        })
        data = r.json().get("data") or []
    except Exception as exc:                                   # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "n": 0}

    # Sanity ceiling. Jaeger stores span duration as an unsigned int, so a
    # span whose end timestamp lands before its start -- clock skew between
    # a container and the host, or a span that never closed cleanly --
    # arrives as ~2^64 microseconds instead of a negative number. One such
    # span appeared in the scaled raw arm and turned max into 1.8e16 ms and
    # the mean into 3.7e13 ms. Percentiles survived it (they are order
    # statistics), but a mean quietly poisoned by one bad row is exactly the
    # kind of number that ends up in a report unchallenged.
    MAX_PLAUSIBLE_MS = 3_600_000.0                             # one hour

    waits, discarded = [], 0
    for trace in data:
        for span in trace.get("spans", []):
            stmt = (_tags(span).get("db.statement") or "")
            if "FOR UPDATE" in stmt:
                ms = span["duration"] / 1000.0
                if 0 <= ms <= MAX_PLAUSIBLE_MS:
                    waits.append(ms)
                else:
                    discarded += 1

    if not waits:
        return {"n": 0, "traces_seen": len(data), "discarded_implausible": discarded}

    waits.sort()

    def pct(p):
        k = max(0, min(len(waits) - 1, int(round(p / 100 * len(waits) + 0.5)) - 1))
        return round(waits[k], 2)

    return {
        "n": len(waits),
        "traces_seen": len(data),
        "discarded_implausible": discarded,
        "p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
        "max_ms": round(waits[-1], 2),
        "mean_ms": round(sum(waits) / len(waits), 2),
    }


# --------------------------------------------------------------------------
def collect(label, lookback_s, window):
    s = scrape()

    per_strategy = {}
    for st in STRATEGIES:
        per_strategy[st] = {
            "confirmed": total(s, "checkout_requests_total", strategy=st, outcome="confirmed"),
            "sold_out": total(s, "checkout_requests_total", strategy=st, outcome="sold_out"),
            "error": total(s, "checkout_requests_total", strategy=st, outcome="error"),
            "occ_retries": total(s, "checkout_retries_total", strategy=st),
            "latency_p50_ms": _ms(hist_quantile(s, "checkout_latency_seconds", 0.50, strategy=st)),
            "latency_p95_ms": _ms(hist_quantile(s, "checkout_latency_seconds", 0.95, strategy=st)),
            "latency_p99_ms": _ms(hist_quantile(s, "checkout_latency_seconds", 0.99, strategy=st)),
        }

    return {
        "label": label,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "checkout": per_strategy,
        "queue": {
            "joins": total(s, "queue_joins_total"),
            "admits": total(s, "queue_admits_total"),
            "wait_p50_s": hist_quantile(s, "queue_wait_seconds", 0.50),
            "wait_p95_s": hist_quantile(s, "queue_wait_seconds", 0.95),
            "wait_p99_s": hist_quantile(s, "queue_wait_seconds", 0.99),
            "admission_rejected": total(s, "admission_rejected_total"),
        },
        "peak_in_flight": peak_in_flight(window),
        "scrape_health": scrape_health(window),
        "lock_wait": lock_waits(lookback_s),
        "instances": metrics_urls(),
    }


def _ms(v):
    return None if v is None else round(v * 1000, 1)


def path_for(label):
    return os.path.join(OUT_DIR, f"queue_comparison_{label}.json")


def _fmt(v, suffix="", nd=1):
    return "--" if v is None else f"{v:,.{nd}f}{suffix}"


def _delta(before, after, lower_is_better=True):
    """ASCII only -- this prints to a Windows console whose default code page
    cannot encode arrows or emoji, and a UnicodeEncodeError in the reporting
    step would lose the whole comparison."""
    if before in (None, 0) or after is None:
        return "--"
    change = (after - before) / before * 100
    arrow = "down" if change < 0 else "up"
    good = (change < 0) == lower_is_better
    return f"{arrow} {abs(change):.0f}%  {'[better]' if good else '[worse]'}"


def report():
    raw, queued = {}, {}
    for label, dest in (("raw", raw), ("queued", queued)):
        p = path_for(label)
        if not os.path.exists(p):
            print(f"missing {p} -- collect that arm first")
            return 1
        dest.update(json.load(open(p, encoding="utf-8")))

    L = []
    L.append(f"raw    collected {raw['collected_at']}")
    L.append(f"queued collected {queued['collected_at']}\n")

    L.append(f"{'metric':<38} {'raw herd':>14} {'through queue':>15}  change")
    L.append("-" * 88)

    rp, qp = raw["peak_in_flight"], queued["peak_in_flight"]
    L.append(f"{'peak checkout_in_flight (total)':<38} "
             f"{_fmt(rp.get('total'), nd=0):>14} {_fmt(qp.get('total'), nd=0):>15}  "
             f"{_delta(rp.get('total'), qp.get('total'))}")

    for st in STRATEGIES:
        r, q = raw["checkout"][st], queued["checkout"][st]
        L.append("")
        L.append(f"  [{st}]")
        for key, label, suffix, lower_better in (
            ("occ_retries", "OCC version conflicts", "", True),
            ("latency_p50_ms", "checkout latency p50", "ms", True),
            ("latency_p95_ms", "checkout latency p95", "ms", True),
            ("confirmed", "confirmed", "", False),
            ("sold_out", "sold out", "", False),
        ):
            if key in ("confirmed", "sold_out") and st != "pessimistic":
                pass
            L.append(f"    {label:<34} {_fmt(r[key], suffix):>14} "
                     f"{_fmt(q[key], suffix):>15}  "
                     f"{_delta(r[key], q[key], lower_better) if lower_better else ''}")

    rl, ql = raw["lock_wait"], queued["lock_wait"]
    L.append("")
    L.append("  [pessimistic lock wait — SELECT ... FOR UPDATE spans, from Jaeger]")
    L.append(f"    {'spans sampled':<34} {_fmt(rl.get('n'), nd=0):>14} "
             f"{_fmt(ql.get('n'), nd=0):>15}")
    for key, label in (("p50_ms", "lock wait p50"), ("p95_ms", "lock wait p95"),
                       ("p99_ms", "lock wait p99"), ("max_ms", "lock wait max")):
        L.append(f"    {label:<34} {_fmt(rl.get(key), 'ms'):>14} "
                 f"{_fmt(ql.get(key), 'ms'):>15}  {_delta(rl.get(key), ql.get(key))}")

    L.append("")
    L.append("  [waiting room]")
    for key, label, suffix in (("joins", "joins", ""), ("admits", "admits", ""),
                               ("wait_p50_s", "queue wait p50", "s"),
                               ("wait_p95_s", "queue wait p95", "s"),
                               ("admission_rejected", "refused at door", "")):
        L.append(f"    {label:<34} {_fmt(raw['queue'][key], suffix):>14} "
                 f"{_fmt(queued['queue'][key], suffix):>15}")

    print("\n".join(L))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", choices=["raw", "queued"],
                   help="which arm was just run")
    p.add_argument("--lookback", type=int, default=600,
                   help="seconds of Jaeger history to scan for lock-wait spans")
    p.add_argument("--window", default="10m",
                   help="Prometheus window for peak in-flight")
    p.add_argument("--report", action="store_true",
                   help="diff the two collected arms")
    args = p.parse_args()

    if args.report:
        return report()
    if not args.label:
        p.error("--label is required unless --report")

    data = collect(args.label, args.lookback, args.window)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path_for(args.label), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"wrote {path_for(args.label)}")
    print(json.dumps(data, indent=2)[:2400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
