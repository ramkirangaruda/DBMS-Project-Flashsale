# Storefront (React + Vite + Tailwind)

A real storefront over the flash-sale API, so the oversell race can be
demonstrated by actual people tapping actual phones instead of by
`app/demos/demo_5_benchmark.py`.

## Run it

Three things need to be up. Backend first:

```bash
docker compose start
python -m scripts.seed                       # canonical demo users + sale
python -m scripts.seed_storefront            # storefront catalogue + a drop
python -m uvicorn app.main:app --port 8010 --host 0.0.0.0
```

Then the frontend:

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
```

`--host 0.0.0.0` on uvicorn and `--host` in the dev script (already in
`package.json`) are what let phones on the same Wi-Fi reach both.

## Running the synchronized drop

This is the demo worth showing.

```bash
python -m scripts.seed_storefront --drop-in 60      # opens 60s from now
python -m scripts.seed_storefront --drop-in 60 --stock 3   # scarcer = crueller
```

1. Get everyone onto the sale page for **Aurora Wireless Earbuds Pro**
   (it's the hero — it always is while it's still upcoming).
2. They'll all see the same `3… 2… 1` countdown. It runs off the **server's**
   clock, not each device's, so phones whose clocks disagree still unlock
   together.
3. Buy Now enables for everyone at the same instant. With stock 3 and six
   people tapping, three get 🎉 and three get 😢.

Re-run the same command to reset stock and schedule another round.

## The hidden strategy switch

Which concurrency strategy Buy Now uses is set by a URL query param, read
once on load and never shown in the UI — ordinary viewers just see a shop.

```
/sale/<id>                        → redis (default)
/sale/<id>?strategy=pessimistic   → SELECT … FOR UPDATE
/sale/<id>?strategy=optimistic    → version-column OCC
```

All three return the same body shape, so the UI never special-cases them:

```json
{ "status": "confirmed" | "sold_out" | "failed",
  "order_id": "…" , "message": "…", "strategy": "…" }
```

## Phones on the LAN

Point them at your machine's IP rather than `localhost`:

```bash
# find it: ipconfig  (Windows)  /  ifconfig  (macOS, Linux)
VITE_API_BASE=http://192.168.1.20:8010 npm run dev
```

Then open `http://192.168.1.20:5173` on each phone. CORS defaults to `*`,
which is fine for a LAN demo — see the note in `app/main.py` about locking
it down for anything real.

## The Engine Room

`/engines` is a separate, story-form page (not part of the shopping flow)
walking through every concurrency/indexing/recovery/ML mechanism this
project runs, each with a small looping SVG diagram -- see
`src/pages/EngineRoom.jsx` and `src/components/engine/`. Linked from the
header as "Engine Room".

## Notes

- Each browser picks one of `scripts/seed.py`'s 30 fixed demo users at
  random and keeps it in `localStorage`, so separate tabs and phones check
  out as different users.
- The sale page polls `GET /sales/{id}` every 700ms; the counter flashes red
  on every change.
- Product imagery is gradients + category glyphs — no assets to ship.
