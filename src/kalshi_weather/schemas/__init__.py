from __future__ import annotations

import json
from importlib import resources
from typing import Any, Mapping

from kalshi_weather.domain.enums import RunMode


SCHEMA_VERSION = "1.0.0"
SCHEMA_RESOURCE = "strategy_decision_explanation_v1.0.0.json"


class SchemaValidationError(ValueError):
    """Raised when a payload does not satisfy the frozen top-level schema."""


def load_strategy_decision_explanation_schema() -> dict[str, Any]:
    with resources.files(__name__).joinpath(SCHEMA_RESOURCE).open(
        "r", encoding="utf-8"
    ) as handle:
        return json.load(handle)


def validate_strategy_decision_explanation_payload(
    payload: Mapping[str, Any],
) -> None:
    schema = load_strategy_decision_explanation_schema()
    required = schema["required"]
    missing = [field for field in required if field not in payload]
    if missing:
        raise SchemaValidationError(f"missing required fields: {', '.join(missing)}")

    unexpected = sorted(set(payload) - set(schema["properties"]))
    if unexpected:
        raise SchemaValidationError(
            f"unexpected top-level fields: {', '.join(unexpected)}"
        )

    if payload["schema_version"] != SCHEMA_VERSION:
        raise SchemaValidationError(
            f"schema_version must be {SCHEMA_VERSION}, got {payload['schema_version']}"
        )

    run_mode = payload["run_mode"]
    if run_mode not in {member.value for member in RunMode}:
        raise SchemaValidationError(f"invalid run_mode: {run_mode}")

    object_fields = (
        "data_freshness",
        "current_state",
        "forecast_summary",
        "path_state",
        "microstructure_summary",
        "edge_summary",
        "regime_summary",
        "risk_summary",
        "module_versions",
    )
    for field_name in object_fields:
        if not isinstance(payload[field_name], Mapping):
            raise SchemaValidationError(f"{field_name} must be an object")

    array_fields = ("explanation_codes", "provenance_refs")
    for field_name in array_fields:
        if not isinstance(payload[field_name], list):
            raise SchemaValidationError(f"{field_name} must be an array")
