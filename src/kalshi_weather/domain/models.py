from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .enums import (
    DecisionType,
    KillSwitchScope,
    QualificationState,
    ReportStatus,
    RiskState,
    RunMode,
    SettlementValidationStatus,
)


JSONMap = dict[str, Any]


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(val) for key, val in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class SeriesDefinition:
    series_ticker: str
    title: str
    city_id: str
    category: str
    frequency: str
    active_from: datetime
    active_to: datetime | None
    source_provenance: tuple[str, ...] = ()


@dataclass(slots=True)
class MarketDefinition:
    market_ticker: str
    event_ticker: str
    series_ticker: str
    market_type: str
    threshold_f: Decimal | None
    operator: str
    open_time: datetime
    close_time: datetime
    settlement_ts: datetime | None
    rules_primary: str
    rules_secondary: str | None
    price_level_structure: str
    price_ranges: tuple[str, ...] = ()
    can_close_early: bool = False
    is_provisional: bool = False
    status: str | None = None


@dataclass(frozen=True, slots=True)
class SettlementRule:
    settlement_rule_id: str
    market_ticker: str
    settlement_variable: str
    # Operator semantics:
    #   ">", ">=", "<", "<="  → single-threshold (use threshold_f)
    #   "between"             → range market (threshold_f = floor, threshold_high_f = cap, both inclusive)
    operator: str
    threshold_f: Decimal | None
    inclusive_flag: bool
    station_id: str
    source_kind: str
    source_locator: str
    local_standard_window_start: datetime
    local_standard_window_end: datetime
    parser_version: str
    validation_status: SettlementValidationStatus
    ambiguity_flags: tuple[str, ...] = ()
    # For "between" operators: upper bound of the inclusive range.
    # None for single-threshold operators.
    threshold_high_f: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StationReference:
    station_id: str
    station_name: str
    metar_code: str
    nws_station_api_id: str
    climate_product_id: str
    latitude: Decimal
    longitude: Decimal
    timezone: str
    wfo_office: str
    grid_x: int
    grid_y: int
    climate_timezone_basis: str
    ncei_station_id: str | None = None
    source_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettlementReportSnapshot:
    station_id: str
    climate_product_id: str
    issue_time: datetime
    report_version: int
    report_status: ReportStatus
    local_standard_window_start: datetime
    local_standard_window_end: datetime
    max_temp_f: int | None
    max_temp_time_local: datetime | None
    min_temp_f: int | None
    raw_text_payload_id: str
    parser_version: str
    revision_flags: tuple[str, ...] = ()
    source_url: str | None = None
    source_kind: str | None = None
    source_locator: str | None = None


@dataclass(frozen=True, slots=True)
class CityProfile:
    city_id: str
    display_name: str
    station_id: str
    region_cluster: str
    marine_sensitive_flag: bool
    onshore_wind_sectors: tuple[int, ...]
    typical_peak_hour_local_by_season: Mapping[str, int]
    heating_window_by_season: Mapping[str, tuple[int, int]]
    cloud_shock_cap_f: Decimal
    storm_shock_cap_f: Decimal
    marine_intrusion_cap_f: Decimal
    wind_shift_cap_f: Decimal
    calibration_buckets_version: str


@dataclass(frozen=True, slots=True)
class ObservationSnapshot:
    station_id: str
    event_time: datetime
    ingest_time: datetime
    temperature_f: Decimal | None
    dewpoint_f: Decimal | None
    wind_dir_deg: int | None
    wind_speed_kt: Decimal | None
    sky_cover_code: str | None
    ceiling_ft: int | None
    visibility_mi: Decimal | None
    weather_codes: tuple[str, ...]
    quality_flags: tuple[str, ...]
    source_payload_id: str


@dataclass(frozen=True, slots=True)
class CurrentStateEstimate:
    as_of_time: datetime
    station_id: str
    current_temp_est_f: Decimal
    current_temp_sigma_f: Decimal
    near_term_slope_f_per_hr: Decimal
    observation_lag_minutes: int
    expected_observation_cadence_minutes: int
    observation_excess_lag_minutes: int
    observation_lag_penalty: Decimal
    shock_risk_score: Decimal
    cloud_cover_shock_adjustment_f: Decimal
    storm_shock_adjustment_f: Decimal
    marine_intrusion_adjustment_f: Decimal
    wind_shift_adjustment_f: Decimal
    discontinuity_suspected: bool
    confidence_downgrade: Decimal
    provenance_refs: tuple[str, ...]
    # T1.3 persistence baseline: yesterday's actual settlement high (°F).
    # When set AND |ensemble_mean − yesterday_high| > 5°F, path engine widens
    # uncertainty (rationale: high regime change = forecast is on shakier
    # ground). None means "no settlement on file" — gracefully degrades.
    yesterday_high_f: Decimal | None = None
    # External-signal observability (2026-05-17). All optional — None means
    # "signal not available, gracefully degrade".
    spc_outlook_rank: int = 0  # 0=none, 2=MRGL, 3=SLGT, 4=ENH, 5=MDT, 6=HIGH
    afd_confidence: str | None = None  # "low" | "moderate" | "high"
    afd_model_spread_flag: bool | None = None
    # Forecaster's stated synoptic regime, extracted by the LLM from the
    # AFD narrative. One of the values in afd_extractor._VALID_REGIMES.
    # Used to bias uncertainty (frontal_passage / convective = +) and
    # nudge the model toward warm/cool when the forecaster calls out
    # an anomaly (anomalous_warm / anomalous_cool).
    afd_regime: str | None = None
    # Forecaster's explicit point estimate for today's high (°F), or None
    # if not stated. Blended with the ensemble mean at a low weight so the
    # forecaster's "human eye" contributes without dominating.
    afd_mentioned_today_high_f: int | None = None


@dataclass(frozen=True, slots=True)
class ForecastSnapshot:
    provider_id: str
    provider_run_time: datetime
    ingest_time: datetime
    valid_for_times: tuple[datetime, ...]
    hourly_temp_path_f: tuple[Decimal, ...]
    cloud_cover_path_pct: tuple[Decimal, ...]
    wind_path: tuple[Decimal, ...]
    precipitation_path: tuple[Decimal, ...]
    provider_metadata: Mapping[str, Any]
    source_payload_id: str


@dataclass(frozen=True, slots=True)
class ForecastDistribution:
    as_of_time: datetime
    station_id: str
    support_temps_f: tuple[int, ...]
    pmf: tuple[Decimal, ...]
    provider_weights: Mapping[str, Decimal]
    base_entropy: Decimal
    sigma_equivalent_f: Decimal
    calibration_version: str
    conditioned_flag: bool


@dataclass(frozen=True, slots=True)
class PathProgressState:
    as_of_time: datetime
    current_high_so_far_f: Decimal
    current_temp_f: Decimal
    threshold_gap_f: Decimal
    remaining_effective_window_minutes: int
    estimated_intraday_slope_f_per_hr: Decimal
    solar_insolation_vector: Mapping[str, Decimal]
    thermal_ceiling_estimate_f: Decimal
    residual_gain_mean_f: Decimal
    residual_gain_p80_f: Decimal
    reachability_score: Decimal
    late_day_decay_factor: Decimal
    path_uncertainty_addon: Decimal
    threshold_already_crossed_flag: bool
    # T1.3 persistence diagnostics — None when yesterday's settlement unknown.
    persistence_gap_f: Decimal | None = None
    persistence_uncertainty_addon: Decimal = Decimal("0")
    # T1.2 climatology shrinkage diagnostics — None when no normals cached.
    p_climatology: Decimal | None = None
    p_pre_shrinkage: Decimal | None = None
    shrinkage_weight: Decimal | None = None


@dataclass(slots=True)
class MarketSnapshot:
    market_ticker: str
    status: str
    open_time: datetime
    close_time: datetime
    settlement_ts: datetime | None
    yes_bid_dollars: Decimal | None
    yes_ask_dollars: Decimal | None
    no_bid_dollars: Decimal | None
    no_ask_dollars: Decimal | None
    yes_bid_size_fp: Decimal | None
    yes_ask_size_fp: Decimal | None
    no_bid_size_fp: Decimal | None
    no_ask_size_fp: Decimal | None
    last_price_dollars: Decimal | None
    last_trade_size_fp: Decimal | None
    volume_fp: Decimal | None
    open_interest_fp: Decimal | None
    updated_time: datetime
    source_payload_id: str


@dataclass(frozen=True, slots=True)
class OrderbookSnapshot:
    market_ticker: str
    as_of_time: datetime
    seq: int
    yes_bids_ladder: tuple[tuple[Decimal, Decimal], ...]
    no_bids_ladder: tuple[tuple[Decimal, Decimal], ...]
    implied_yes_asks_ladder: tuple[tuple[Decimal, Decimal], ...]
    implied_no_asks_ladder: tuple[tuple[Decimal, Decimal], ...]
    checksum_status: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    trade_id: str
    market_ticker: str
    created_time: datetime
    count_fp: Decimal
    yes_price_dollars: Decimal
    no_price_dollars: Decimal
    taker_side: str
    source_payload_id: str


@dataclass(frozen=True, slots=True)
class MicrostructureAssessment:
    market_ticker: str
    side: str
    quantity_fp: Decimal
    top_of_book_price: Decimal | None
    executable_wap_price: Decimal | None
    depth_consumed_levels: int
    slippage_cost: Decimal
    top_of_book_durability_score: Decimal
    quote_stability_score: Decimal
    ghost_liquidity_ratio: Decimal
    maker_fill_probability: Decimal
    maker_adverse_selection_penalty: Decimal
    taker_adverse_selection_penalty: Decimal
    time_to_close_bucket: str


@dataclass(frozen=True, slots=True)
class TradabilityAssessment:
    market_ticker: str
    side: str
    tradability_score: Decimal
    thin_book_flag: bool
    stale_book_flag: bool
    friction_overload_flag: bool
    allowed_taker_flag: bool
    allowed_maker_flag: bool
    block_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EdgeEstimate:
    market_ticker: str
    side: str
    quantity_fp: Decimal
    p_model: Decimal
    p_market_exec: Decimal
    raw_edge: Decimal
    fee_cost: Decimal
    slippage_cost: Decimal
    adverse_selection_penalty: Decimal
    total_friction: Decimal
    friction_to_edge_ratio: Decimal
    uncertainty_haircut: Decimal
    regime_haircut: Decimal
    portfolio_haircut: Decimal
    edge_conf_adj: Decimal
    executable_ev_per_contract: Decimal
    executable_ev_total: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    plan_id: str
    action_type: DecisionType
    market_ticker: str
    side: str
    quantity_fp: Decimal
    order_type: str
    limit_price_dollars: Decimal | None
    time_in_force: str
    max_cost_dollars: Decimal | None
    cancel_on_pause: bool
    maker_flag: bool
    rationale_codes: tuple[str, ...]
    expires_at: datetime | None
    run_mode: RunMode
    decision_utc: datetime
    max_age_seconds: int = 45
    portfolio_haircut: Decimal = Decimal("0")
    active_city_count: int = 1


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    city_id: str
    as_of_time: datetime
    active_regime: str
    regime_scores: Mapping[str, Decimal]
    feature_values: Mapping[str, Any]
    block_flag: bool
    haircut_value: Decimal
    sizing_multiplier: Decimal
    explanation_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskDecision:
    risk_state: RiskState
    hard_halt_flag: bool
    city_halt_flag: bool
    global_halt_flag: bool
    exposure_ok: bool
    correlation_ok: bool
    source_health_ok: bool
    qualification_ok: bool
    veto_reasons: tuple[str, ...]
    manual_override_state: str | None


@dataclass(frozen=True, slots=True)
class StrategyDecisionExplanation:
    schema_version: str
    decision_id: str
    run_mode: RunMode
    as_of_time: datetime
    market_ticker: str
    city_id: str
    station_id: str
    settlement_rule_id: str
    data_freshness: JSONMap
    current_state: JSONMap
    forecast_summary: JSONMap
    path_state: JSONMap
    microstructure_summary: JSONMap
    edge_summary: JSONMap
    regime_summary: JSONMap
    risk_summary: JSONMap
    final_decision: DecisionType
    explanation_codes: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    module_versions: Mapping[str, str]

    def to_dict(self) -> JSONMap:
        return _serialize(asdict(self))


@dataclass(slots=True)
class PositionState:
    market_ticker: str
    side: str
    quantity_fp: Decimal
    avg_entry_price_dollars: Decimal
    fees_paid_dollars: Decimal
    realized_pnl_dollars: Decimal
    unrealized_mark_dollars: Decimal
    opened_at: datetime
    last_updated_at: datetime
    source: str


@dataclass(slots=True)
class ShadowFill:
    shadow_fill_id: str
    decision_id: str
    market_ticker: str
    side: str
    quantity_fp: Decimal
    modeled_fill_price_dollars: Decimal
    fill_scenario: str
    fill_confidence: Decimal
    modeled_fee_dollars: Decimal
    modeled_slippage_dollars: Decimal
    modeled_adverse_selection_dollars: Decimal
    fill_time: datetime
    reconciliation_status: str
    predicted_executable_ev_per_contract: Decimal | None = None
    predicted_executable_ev_total: Decimal | None = None
    model_confidence: Decimal | None = None
    execution_confidence: Decimal | None = None
    governance_confidence: Decimal | None = None
    overall_trade_confidence: Decimal | None = None
    confidence_reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class ShadowPosition:
    city_id: str
    market_ticker: str
    side: str
    open_quantity_fp: Decimal
    avg_cost_dollars: Decimal
    cumulative_fees_dollars: Decimal
    mark_pnl_dollars: Decimal
    settled_pnl_dollars: Decimal
    lifecycle_status: str


@dataclass(slots=True)
class CityQualificationState:
    city_id: str
    state: QualificationState
    effective_from: datetime
    effective_to: datetime | None
    settlement_validation_score: Decimal
    calibration_score: Decimal
    nowcast_score: Decimal
    path_score: Decimal
    market_depth_score: Decimal
    slippage_score: Decimal
    shadow_ev_score: Decimal
    drawdown_score: Decimal
    promotion_reasons: tuple[str, ...]
    demotion_reasons: tuple[str, ...]


@dataclass(slots=True)
class KillSwitchState:
    scope: KillSwitchScope
    state: str
    triggered_at: datetime
    trigger_reason: str
    cleared_at: datetime | None
    cleared_by: str | None
    sticky_flag: bool
