# Kalshi Weather Test

Phase 1 foundation for the settlement-truth, shadow-first weather-trading platform described in [docs/kalshi_weather_architecture_spec.md](docs/kalshi_weather_architecture_spec.md).

Current implementation scope:

- typed domain models for the core architecture objects
- frozen `StrategyDecisionExplanation` schema `1.0.0`
- settlement CLI parser with explicit `MAXIMUM` parsing
- settlement revision monitor scaffolding
- settlement rule parser and market-vs-report validation helpers
- ingestion contracts and timing-drift monitoring
- file-backed raw payload store and reference registry bootstrap
- public NWS CLI, observation, hourly forecast, grid forecast, and Kalshi market clients and adapters
- authenticated Kalshi WebSocket client and stream parsers for orderbook snapshots/deltas and public trades
- exchange capability check scaffolding for current Kalshi public endpoints
- decision, risk, microstructure, path, forecast-calibration, and shadow-first execution engines
- replay-light, shadow reconciliation, settlement corpus validation, and live-gating reports
- authenticated Kalshi live client scaffolding with signed request support
- unit tests for the Phase 1 and shadow-first implementation

## Layout

- `src/kalshi_weather/domain`: enums and dataclasses
- `src/kalshi_weather/settlement`: climate report parsing and revision monitoring
- `src/kalshi_weather/ingestion`: adapter contracts and source-health helpers
- `src/kalshi_weather/engines`: forecast, nowcast, path, EV, risk, shadow, and reconciliation engines
- `src/kalshi_weather/analytics`: shadow, sensitivity, forecast-calibration, nowcast, drift, and live-gating reports
- `src/kalshi_weather/live`: thin live adapter and signed Kalshi private-client boundary
- `src/kalshi_weather/clients/kalshi_ws.py`: authenticated Kalshi WebSocket connection layer
- `src/kalshi_weather/checks`: startup exchange capability checks
- `src/kalshi_weather/schemas`: frozen JSON schema artifacts
- `src/kalshi_weather/tools`: runnable bootstrap, cycle, replay, validation, and reporting tools

## Run Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Run Public Capability Check

```bash
PYTHONPATH=src python3 -m kalshi_weather.checks.exchange_capability
```

## Bootstrap Local Reference Seed

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.bootstrap_reference
```

This writes a local runtime seed under `data/reference/registry.json`. The current default seed includes only NYC and is intentionally narrow.

## Fetch and Normalize Current NYC CLI

```bash
PYTHONPATH=src python3 - <<'PY'
from kalshi_weather.clients.nws import NwsClimateClient
from kalshi_weather.ingestion.adapters import NwsCliAdapter
from kalshi_weather.storage.reference_registry import FileReferenceRegistry
seed = FileReferenceRegistry.default_seed()
station = seed.stations[0]
report = NwsCliAdapter(NwsClimateClient(), station=station, issuedby="NYC").normalize(
    NwsCliAdapter(NwsClimateClient(), station=station, issuedby="NYC").fetch_raw()
)[0].record
print(report)
PY
```

## Sample Settlement Wiring Check

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.validate_settlement_sample
```

This is a wiring sample only. It does not claim historical settlement validation because a historical CLI archive source is not yet wired.

## Run A Shadow Decision Cycle

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.run_city_cycle
```

This pulls current NYC observations, forecast, open markets, and orderbooks, writes raw payloads and normalized state, emits decision explanations, and applies shadow fills only when the risk and qualification gates allow it.

The cycle now ingests both NWS hourly and `forecastGridData` forecasts, writes a provider-reliability report under `data/derived/provider_reliability/`, and feeds those weights back into the forecast engine.

## Refresh Forecast Calibration

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.update_forecast_calibration
```

This recomputes the stored provider-reliability report from normalized forecasts versus realized observations. The current MVP uses those weights only as an empirical reliability input, not as a live-eligibility override.

## Capture Streaming Market Data

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.capture_market_stream
```

This opens an authenticated Kalshi WebSocket session, subscribes to `orderbook_delta` and `trade` for current `KXHIGHNY` open markets, stores raw frames, and updates normalized orderbook and trade state. It requires the same authenticated environment variables as the thin live adapter.

## Run A Live-Readonly Stream Decision Loop

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.run_live_readonly_stream
```

This bootstraps the latest observation and forecast state, consumes authenticated Kalshi WebSocket orderbook and trade updates, and emits `LIVE_READONLY` decision explanations without placing any orders. It is the current streaming path for validating event-driven decision behavior before any live-trading adapter is allowed to submit.

## Refresh The Empirical Blockers

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.refresh_empirical_blockers
```

This runs observation backfill, CLI archive refresh, forecast-calibration refresh, settlement validation, nowcast validation, a fresh decision cycle, and qualification/live-gating reporting in one pass.

## Archive Weather State Overnight

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.archive_weather_loop --iterations 8 --sleep-seconds 900
```

This repeatedly archives official observations and forecast snapshots so forecast-calibration sample depth can continue growing overnight.

## Run The Validation Reports

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.shadow_report
PYTHONPATH=src python3 -m kalshi_weather.tools.validate_nowcast_bridge
PYTHONPATH=src python3 -m kalshi_weather.tools.validate_settlement_corpus
```

These produce the nowcast baseline comparison, settlement-corpus summary, sensitivity matrix, drift report, and live gate report. The system is expected to block live trading while those reports show unresolved gaps.

## Reconcile Shadow Positions

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.reconcile_shadow_positions
```

This closes open shadow positions only when the exchange market is finalized and a deterministic result is available.

## Thin Live Adapter

The codebase now includes a signed Kalshi private-client boundary and a thin live adapter. Real order submission remains gate-controlled:

- `LIVE_TRADE` run mode is required.
- live-gating must pass
- a manual kill switch must be inactive
- authenticated Kalshi credentials must be configured

Environment variables for authenticated requests:

```bash
export KALSHI_ACCESS_KEY="..."
export KALSHI_PRIVATE_KEY_PATH="/absolute/path/to/key.pem"
export KALSHI_ENV="demo"   # or prod
export KALSHI_SUBACCOUNT="0"
```

The safest runtime check remains dry-run payload preparation:

```bash
PYTHONPATH=src python3 -m kalshi_weather.tools.prepare_live_plan
```
