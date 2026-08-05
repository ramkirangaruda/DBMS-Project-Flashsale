"""
Screenshot the provisioned Grafana dashboard (headless Chromium).

A capture utility for the observability write-up, not part of the app.

    python -m scripts.capture_dashboard results/dashboards/n200.png --window 15m

Grafana runs with anonymous admin access in docker-compose.yml, so there is
no login step to automate.
"""
import os
import sys
import time

DASHBOARD_UID = "flashsale-chaos"
GRAFANA = os.getenv("GRAFANA_URL", "http://127.0.0.1:3000")


def capture(out_path, window="15m", width=1920, height=1120, settle_s=9.0):
    from playwright.sync_api import sync_playwright

    url = (f"{GRAFANA}/d/{DASHBOARD_UID}/?orgId=1&from=now-{window}&to=now"
           f"&refresh=2s&kiosk")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-device-scale-factor=1"])
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=1.5)
        page.goto(url, wait_until="networkidle", timeout=90_000)
        # Panels render asynchronously after the initial load settles; without
        # this the capture catches half-drawn charts and empty legends.
        time.sleep(settle_s)
        try:
            page.wait_for_selector("[data-testid='data-testid panel content']",
                                   timeout=20_000)
        except Exception:                                      # noqa: BLE001
            pass
        time.sleep(2.0)
        page.screenshot(path=out_path, full_page=True)
        browser.close()
    return out_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "results/dashboards/dashboard.png"
    window = "15m"
    if "--window" in sys.argv:
        window = sys.argv[sys.argv.index("--window") + 1]
    path = capture(out, window=window)
    size = os.path.getsize(path)
    print(f"saved {path} ({size:,} bytes)")
