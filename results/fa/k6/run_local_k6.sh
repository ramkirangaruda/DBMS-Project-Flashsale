#!/usr/bin/env bash
# Fresh single-replica k6 baseline on this machine (research/DECISIONS.md D2).
# Usage: results/fa/k6/run_local_k6.sh <strategy> <script.js> <out.json> [extra k6 -e args...]
# Needs: Postgres 5435 + Redis 6390 seeded (python -m scripts.seed), k6 on PATH or K6=/path/to/k6.
set -euo pipefail
STRAT=$1; SCRIPT=$2; OUT=$3; shift 3
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
K6=${K6:-k6}; PY=${PY:-python}
SALE=22222222-2222-2222-2222-222222222222
cd "$ROOT"
for p in 'queue:*' 'admission:*' 'admctl:*' 'stock:*' 'idempotency:*' 'fa:*' 'dblim:*'; do
  redis-cli -p 6390 --scan --pattern "$p" | xargs -r redis-cli -p 6390 del >/dev/null
done
psql -h localhost -p 5435 -U postgres -d flashsale -qc \
  "UPDATE flash_sale_events SET start_time = now() - interval '1 hour', end_time = now() + interval '3 hours' WHERE id = '$SALE'"
OTEL_SDK_DISABLED=true SALE_WINDOW_ENFORCED=false PYTHONPATH="$ROOT" \
  $PY -c "import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', port=8010, log_level='warning')" \
  > results/fa/logs/k6_server.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 60); do curl -sf http://127.0.0.1:8010/api >/dev/null && break; sleep 0.5; done
curl -sf -X POST "http://127.0.0.1:8010/admin/reset-sale/$SALE?stock=60" >/dev/null
curl -sf -X POST "http://127.0.0.1:8010/queue/reset/$SALE" >/dev/null
BEFORE=$(psql -h localhost -p 5435 -U postgres -d flashsale -Atc "select count(*) from orders where sale_id='$SALE'")
cp results/load/users.json "$(dirname "$SCRIPT")/users.json" 2>/dev/null || true
"$K6" run -q -e BASE_URL=http://127.0.0.1:8010 -e STRATEGY="$STRAT" -e SALE_ID="$SALE" -e N=1000 \
  -e USE_QUEUE=true -e RUN_NONCE="k6local-$(date +%s)" -e SUMMARY_PATH="$OUT" "$@" "$SCRIPT"
AFTER=$(psql -h localhost -p 5435 -U postgres -d flashsale -Atc "select count(*) from orders where sale_id='$SALE'")
INV=$(psql -h localhost -p 5435 -U postgres -d flashsale -Atc "select reserved_stock, total_stock from inventory where sale_id='$SALE'")
echo "orders created: $((AFTER - BEFORE)); inventory reserved|total: $INV"
