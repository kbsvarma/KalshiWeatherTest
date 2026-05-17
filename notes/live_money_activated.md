# Live Money Mode Activated — 2026-05-16 evening ET

The bot is now configured to place **real Kalshi orders** on qualifying signals
from the autonomous 30-minute cycle. Activated per user request after the
4-bet hypothetical showed +$2.77 expected EV on $1.23 stake.

## What Changed

- `engines/live_execution.py` — new module that mirrors each successful shadow
  fill with a real Kalshi order via `KalshiPrivateClient.create_order`.
- `tools/run_city_cycle.py` — after each `apply_shadow_decision` that returns
  a fill, calls `maybe_place_live_order` with the same decision + edge.
- `scripts/run_weather_cycle.sh` — sets `LIVE_ORDERS_ENABLED=1` and
  `LIVE_ORDERS_DRY_RUN=0` so the launchd-driven 30-min cycles place real
  orders.

## Hard Risk Limits (in code, cannot be bypassed by env)

| Limit | Value | Where enforced |
|---|---|---|
| Contracts per market per day | **1** | `MAX_CONTRACTS_PER_MARKET` constant |
| Daily $$ exposure cap | **$10** | `LIVE_DAILY_USD_CAP` env (default 10) |
| Min remaining balance | **$1.00** | `MIN_REMAINING_BALANCE_USD` constant |
| Same-market dedup | yes | `_market_already_traded_today` |
| Kill switch honored | yes | `_check_kill_switch` |
| Price range | $0.01–$0.99 | early reject if outside |
| Time in force | **IOC** | order is killed if not filled immediately |

## What Happens On The Next Qualifying Signal

When the 30-min cycle fires and ANY market produces a TAKER_ALLOWED decision
that passes the volume-grinder shadow-fill gates:

1. Decision engine emits TAKER_ALLOWED with side + executable EV
2. `apply_shadow_decision` records a shadow fill (existing behaviour)
3. `maybe_place_live_order` is invoked:
   - Checks gates above
   - Builds order: 1 contract, limit at modeled price, IOC
   - Calls `KalshiPrivateClient.create_order`
   - Records the result in `live_orders` table
4. Cycle log line: `[LIVE-EXEC] ✓ PLACED <ticker> <side> 1@<price>c`

## Reverting To Safe Mode

Set the wrapper env vars and re-load launchd:

```bash
# Soft revert — preview-only, no real orders
sed -i '' 's/LIVE_ORDERS_DRY_RUN:=0/LIVE_ORDERS_DRY_RUN:=1/' \
  scripts/run_weather_cycle.sh

# Hard revert — disable entirely
sed -i '' 's/LIVE_ORDERS_ENABLED:=1/LIVE_ORDERS_ENABLED:=0/' \
  scripts/run_weather_cycle.sh
```

Or just remove the export lines from the wrapper.

## Audit Trail

Every live order attempt — success, dry-run, blocked, or error — gets a row
in the `live_orders` table with full payload. Query examples:

```sql
-- All live orders placed today
SELECT created_at, market_ticker, status, payload_json
FROM live_orders
WHERE substr(created_at, 1, 10) = date('now', 'utc')
ORDER BY created_at DESC;

-- Total live spend today
SELECT SUM(json_extract(payload_json,'$.yes_price')/100.0 * json_extract(payload_json,'$.count'))
FROM live_orders
WHERE substr(created_at, 1, 10) = date('now', 'utc') AND status LIKE 'PLACED%';
```
