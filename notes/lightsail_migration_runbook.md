# Lightsail Migration Runbook

**Status**: All artifacts prepared. Awaiting SSH window to execute.

## Artifacts in this repo

| File | Purpose |
|---|---|
| `scripts/lightsail/migrate_all.sh` | **Master script** — one-shot full migration |
| `scripts/lightsail/bootstrap.sh` | Cloud-side: user, venv, Ollama, systemd units |
| `scripts/lightsail/migrate_state.sh` | DB + secrets copy (Mac → Lightsail) |
| `scripts/lightsail/flip_to_live.sh` | Toggle paper → live (interactive confirmation) |
| `scripts/lib/portable.sh` | BSD↔GNU portability shim (stat, date) |
| `deploy/systemd/kalshi-weather-cycle.{service,timer}` | Decision cycle every 30 min |
| `deploy/systemd/kalshi-weather-settlements.{service,timer}` | Daily morning settlement ingest |
| `deploy/systemd/kalshi-weather-watchdog.{service,timer}` | Watchdog at :08 and :38 |
| `deploy/systemd/kalshi-weather-dashboard.service` | Streamlit on port 8501 |

## What's already done (Stage 0)

- Refactored 6 shell scripts to be platform-portable
- Created `scripts/lib/portable.sh` for BSD↔GNU `stat`/`date` differences
- `ROOT` and `PY` paths are now env-overridable
- `launchctl` calls guarded by `_is_darwin`
- All 160 unit + integration tests pass on Mac (1 skipped during UTC midnight window)
- Mac behavior unchanged — defaults preserve current values

## What to run to migrate (one command)

```bash
cd /Users/varmakammili/Documents/GitHub/KalshiWeatherTest
bash scripts/lightsail/migrate_all.sh
```

That script:
1. Verifies SSH to `54.225.195.11`
2. Rsyncs the repo to `/opt/kalshi-weather` on Lightsail (excludes `.git`, venv, runtime DB)
3. Runs `bootstrap.sh` (installs OS packages, creates `kalshibot` system user, sets up venv with `pip install -r requirements.txt`, installs Ollama + pulls `llama3.2:3b`, installs systemd units, enables timers)
4. Copies state DB + recent cycle reports + Kalshi private key
5. Triggers one validation cycle, tails the log for 90s
6. Prints monitoring cheat sheet

**Default state after migration**:
- `LIVE_ORDERS_ENABLED=0`, `LIVE_ORDERS_DRY_RUN=1` — hard-coded in the systemd unit
- Mac launchd cycle is NOT touched — Mac continues live, Lightsail dry-runs in parallel
- Dashboard runs on Lightsail port 8501 (need to open via `aws lightsail open-instance-public-ports`)

## After migration, validate

```bash
# SSH in
ssh -i ~/.ssh/lightsail.pem ubuntu@54.225.195.11

# Check timer is firing
sudo systemctl list-timers --no-pager | grep kalshi-weather

# Watch a cycle live (next one at :00 or :30 UTC)
sudo journalctl -u kalshi-weather-cycle.service -f

# View the last cycle's report
tail -1 /opt/kalshi-weather/logs/cycle_reports/$(date -u +%Y-%m-%d).jsonl | jq .

# Confirm it's dry-running (should see DRY_RUN_PREVIEW status rows)
sudo -u kalshibot sqlite3 /opt/kalshi-weather/data/state/runtime.sqlite3 \
  "SELECT status, count(*) FROM live_orders WHERE substr(created_at,1,10)=date('now') GROUP BY status;"
```

## Comparison protocol (paper vs live)

Run for **at least 3 cycles** in parallel. For each cycle:

1. Note what Mac places (real orders) in `live_orders` with `status LIKE 'PLACED_%'`
2. Note what Lightsail would-place (preview rows) with `status='DRY_RUN_PREVIEW'`
3. They should match — same tickers, same sides, same prices

If they diverge, investigate before flipping.

## When ready to flip live

```bash
bash scripts/lightsail/flip_to_live.sh
```

That script:
1. Pre-flight: confirms SSH, ≥3 dry-run cycles, Mac launchd state, operator confirmation
2. **Disables Mac launchd cycle first** (so there's never overlap)
3. Drops a systemd override on Lightsail: `LIVE_ORDERS_ENABLED=1, LIVE_ORDERS_DRY_RUN=0`
4. Restarts the cycle service
5. Tails the first post-flip cycle

## Rollback

```bash
bash scripts/lightsail/flip_to_live.sh --revert
```

That script:
1. Removes the systemd override on Lightsail (back to paper)
2. Re-enables Mac launchd cycle

## Open ports for dashboard (one-time)

```bash
aws lightsail open-instance-public-ports \
  --instance-name kalshi-bot \
  --port-info fromPort=8501,toPort=8501,protocol=TCP,cidrs=["YOUR_IP/32"] \
  --region us-east-1
```

Or use SSH tunnel (zero firewall change):
```bash
ssh -L 8501:127.0.0.1:8501 -i ~/.ssh/lightsail.pem ubuntu@54.225.195.11
# open http://127.0.0.1:8501
```

## Troubleshooting

**Cycle fails with "Ollama unavailable":** non-fatal. AFD signal degrades gracefully. Bot continues without AFD signal. Check `sudo systemctl status ollama` and `curl http://localhost:11434/api/tags`.

**Cycle hits 900s timeout:** check `/opt/kalshi-weather/logs/cron_cycle.log` for HTTP 429 from Open-Meteo. Circuit breaker should trip after first 429. If sustained, consider Hobbyist tier.

**Watchdog writes false FAILED:** check `/opt/kalshi-weather/logs/watchdog.log` for the diagnostic dump I added earlier. Should show `slot_epoch`, `last_end_epoch`, heartbeat contents.

**State DB locked:** SQLite has WAL mode, but if you see "database is locked" errors, stop the dashboard service briefly: `sudo systemctl stop kalshi-weather-dashboard`, run the failing operation, restart.

## Network note

Comcast↔AWS routing from this Mac (`68.80.186.101`) to Lightsail (`54.225.195.11`) has been intermittent. If `ssh` times out:
- Try EC2 Instance Connect via AWS console (browser SSH)
- Or run from a different network (mobile hotspot)
- Or wait — flakes usually clear within hours
