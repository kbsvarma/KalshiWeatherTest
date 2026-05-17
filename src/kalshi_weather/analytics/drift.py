from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any


def build_drift_report(
    decision_payloads: list[dict[str, Any]],
    fill_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    decision_counter = Counter(payload.get("final_decision", "UNKNOWN") for payload in decision_payloads)
    schema_versions = Counter(payload.get("schema_version", "unknown") for payload in decision_payloads)
    observation_lags = [
        payload.get("data_freshness", {}).get("observation_lag_minutes")
        for payload in decision_payloads
        if payload.get("data_freshness", {}).get("observation_lag_minutes") is not None
    ]
    excess_lags = [
        payload.get("data_freshness", {}).get("observation_excess_lag_minutes")
        for payload in decision_payloads
        if payload.get("data_freshness", {}).get("observation_excess_lag_minutes") is not None
    ]
    average_lag = (sum(observation_lags) / len(observation_lags)) if observation_lags else None
    average_excess_lag = (sum(excess_lags) / len(excess_lags)) if excess_lags else None
    p95_excess_lag = None
    median_lag = None
    if excess_lags:
        ordered_excess = sorted(float(value) for value in excess_lags)
        p95_excess_lag = ordered_excess[min(len(ordered_excess) - 1, int(len(ordered_excess) * 0.95))]
    if observation_lags:
        median_lag = median(float(value) for value in observation_lags)
    fill_reconciliation = Counter(
        payload.get("reconciliation_status", "UNKNOWN") for payload in (fill_payloads or [])
    )
    return {
        "decision_distribution": dict(decision_counter),
        "schema_versions": dict(schema_versions),
        "average_observation_lag_minutes": average_lag,
        "median_observation_lag_minutes": median_lag,
        "average_observation_excess_lag_minutes": average_excess_lag,
        "p95_observation_excess_lag_minutes": p95_excess_lag,
        "fill_reconciliation_distribution": dict(fill_reconciliation),
    }
