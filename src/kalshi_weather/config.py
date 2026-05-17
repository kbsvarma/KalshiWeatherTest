from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarketGuardrails:
    max_cities: int = 3
    one_position_per_city: bool = True
    max_live_contracts_per_trade: Decimal = Decimal("1")
    max_total_open_exposure_contracts: Decimal = Decimal("3")
    default_run_mode: str = "SHADOW"
    # Kelly sizing: recommended contracts = clamp(floor(exec_ev / kelly_ev_step), min, max)
    # e.g. exec_ev=0.06 with step=0.02 → 3 contracts (capped at kelly_max_contracts)
    kelly_min_contracts: int = 1
    kelly_max_contracts: int = 3
    kelly_ev_step: Decimal = Decimal("0.020")  # each step above floor adds 1 contract


@dataclass(frozen=True, slots=True)
class SettlementMonitorSettings:
    active_local_pull_interval_minutes: int = 15
    final_monitor_start_hour_local: int = 0
    final_monitor_start_minute_local: int = 15
    final_monitor_end_hour_local: int = 5
    final_monitor_end_minute_local: int = 15
    required_stability_minutes: int = 90


@dataclass(frozen=True, slots=True)
class SourceHealthSettings:
    observation_soft_stale_minutes: int = 12
    observation_hard_stale_minutes: int = 25
    orderbook_soft_stale_seconds: int = 3
    orderbook_hard_stale_seconds: int = 15
    timing_drift_warning_seconds: int = 600


@dataclass(frozen=True, slots=True)
class AppConfig:
    guardrails: MarketGuardrails = field(default_factory=MarketGuardrails)
    settlement_monitor: SettlementMonitorSettings = field(
        default_factory=SettlementMonitorSettings
    )
    source_health: SourceHealthSettings = field(default_factory=SourceHealthSettings)


DEFAULT_CONFIG = AppConfig()
