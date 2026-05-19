#!/usr/bin/env python3
"""Print a JSON snapshot of the Kalshi account: cash balance + open exposure.

Used by the dashboard (which deliberately does NOT import from src/) via
subprocess. On any failure, prints a JSON object with an "error" key so
the dashboard can render a graceful "—" instead of crashing.

Output schema (success):
  {"balance_usd": 47.89, "open_positions": 27, "open_exposure_usd": 16.04}

Output schema (failure):
  {"error": "<message>"}
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

# Locate the repo and import the bot's Kalshi client. This script lives
# under scripts/ — the bot code lives under src/kalshi_weather/. We import
# the client here (not from dashboard.py) to keep the dashboard process
# import-clean per its design constraint.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# Fall back to the credentials documented in scripts/run_weather_cycle.sh
# so the dashboard works without manually exporting env vars. These are
# already on disk in plaintext in the wrapper; nothing new is leaked.
os.environ.setdefault("KALSHI_API_KEY_ID", "2d618372-e5bb-4515-a5a5-0e41b4717ad6")
os.environ.setdefault(
    "KALSHI_PRIVATE_KEY_PATH",
    str(Path.home() / ".kalshi" / "private_key.pem"),
)


def main() -> int:
    try:
        from kalshi_weather.clients.kalshi_private import KalshiPrivateClient

        client = KalshiPrivateClient.from_env()
        bal = client.get_balance()
        pos = client.list_positions(limit=500)

        # Balance is in integer cents under the "balance" field.
        balance_cents = int(bal.get("balance", 0))
        balance_usd = balance_cents / 100.0

        # Open exposure = sum of market_exposure_dollars across positions
        # where the position is not flat (position_fp != 0). Closed/settled
        # positions remain in the response but with position_fp=0.
        market_positions = pos.get("market_positions", []) or []
        open_count = 0
        exposure = Decimal("0")
        for mp in market_positions:
            try:
                position_fp = Decimal(str(mp.get("position_fp", "0") or "0"))
            except Exception:
                position_fp = Decimal("0")
            if position_fp != 0:
                open_count += 1
                try:
                    exposure += Decimal(
                        str(mp.get("market_exposure_dollars", "0") or "0")
                    )
                except Exception:
                    pass

        out = {
            "balance_usd": round(balance_usd, 2),
            "open_positions": open_count,
            "open_exposure_usd": float(round(exposure, 2)),
        }
        print(json.dumps(out))
        return 0
    except Exception as exc:  # pragma: no cover — safety net
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
