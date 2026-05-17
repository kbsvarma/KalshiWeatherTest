from __future__ import annotations

from datetime import datetime, timezone
import unittest

from kalshi_weather.schemas import (
    SCHEMA_VERSION,
    SchemaValidationError,
    validate_strategy_decision_explanation_payload,
)


def _payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": "dec-1",
        "run_mode": "SHADOW",
        "as_of_time": datetime.now(timezone.utc).isoformat(),
        "market_ticker": "KXHIGHNY-26APR06-T61",
        "city_id": "nyc",
        "station_id": "nyc-central-park",
        "settlement_rule_id": "rule-1",
        "data_freshness": {},
        "current_state": {},
        "forecast_summary": {},
        "path_state": {},
        "microstructure_summary": {},
        "edge_summary": {},
        "regime_summary": {},
        "risk_summary": {},
        "final_decision": "NO_TRADE",
        "explanation_codes": ["default_no_trade"],
        "provenance_refs": ["raw:1"],
        "module_versions": {"decision": "1"},
    }


class SchemaTest(unittest.TestCase):
    def test_valid_payload_passes(self) -> None:
        validate_strategy_decision_explanation_payload(_payload())

    def test_missing_field_fails(self) -> None:
        payload = _payload()
        payload.pop("risk_summary")
        with self.assertRaises(SchemaValidationError):
            validate_strategy_decision_explanation_payload(payload)

    def test_unexpected_field_fails(self) -> None:
        payload = _payload()
        payload["extra"] = True
        with self.assertRaises(SchemaValidationError):
            validate_strategy_decision_explanation_payload(payload)


if __name__ == "__main__":
    unittest.main()
