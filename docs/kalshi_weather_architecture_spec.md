# Kalshi Weather Trading Platform Architecture Specification

Status: final pre-code architecture specification

Scope: daily high-temperature threshold markets on Kalshi, research-grade and shadow-first, with a thin live adapter intentionally deferred behind empirical gates

Date: 2026-04-05

## 1. Executive Summary

This system is materially stronger than a naive weather bot because it is built around the exact settlement variable, the intraday path to that settlement, and the size-aware executable market price rather than an app temperature estimate and top-of-book quote. It treats the daily high as a path-dependent state variable, explicitly models stale observations and sudden discontinuities, prices real frictions, and allows governance vetoes to override model confidence.

It is still intentionally narrow. MVP supports at most three cities, only single-threshold binary markets, only one position per city, and taker-first realism. The product is not live trading. The product is a replayable, auditable, shadow-first decision system that can later expose a thin live order adapter only after settlement reconciliation, calibration, nowcast accuracy, fill realism, and shadow EV gates are all passed.

## 2. Final System Thesis

Trade only when a settlement-truth, path-aware probability for the official daily high exceeds the market's size-aware executable implied probability by enough to survive fees, slippage, adverse selection, uncertainty, convergence decay, portfolio correlation, and governance constraints; otherwise do nothing.

## 3. Design Principles

- Settlement truth beats consumer weather truth.
- The modeled variable is the exact official settlement variable, not a proxy.
- The daily high is path-dependent, not a static endpoint.
- Current-state bridging and settlement forecasting are separate modules.
- Executable EV beats theoretical edge.
- NO_TRADE is the default.
- Risk and governance can veto any trade.
- Raw, normalized, and derived data are strictly separated.
- Every payload, parser, feature, and decision must carry provenance.
- Qualification logic must ignore incentives and rebates.
- MVP thresholds may be provisional, but every provisional threshold needs a replacement dataset and promotion rule.
- Regimes must be few, measurable, and action-linked.
- Shadow is both validation and model feedback.
- Unknowns must be isolated, monitored, and bounded.
- Every decision must emit a versioned machine-readable explanation object.

## 4. Final Target Architecture

End-to-end flow:

1. Universe selection loads a fixed list of up to three approved city series and filters for active single-threshold binary markets only.
2. Reference services resolve market metadata, settlement rules, station references, city profiles, fee schedule version, price grid, and parser versions.
3. Ingestion adapters capture official observations, official forecasts, deterministic short-range model data, ensemble data, Kalshi market metadata, orderbook snapshots and deltas, public trades, and replay archives.
4. Raw payloads are stored append-only with source timestamps, ingest timestamps, request metadata, parser version, and payload hashes.
5. Normalizers convert raw payloads into canonical weather, market, and orderbook records without deriving strategy features.
6. Settlement Rule Resolver converts each market into an exact settlement specification: station, observation window, comparison operator, threshold, source locator, local-standard-time boundary semantics, and validation status.
7. Before any strategy logic is trusted, a settlement validation harness must compare parsed official settlement truth against historical Kalshi settled outcomes and block the market if any mismatch remains unresolved. The harness must explicitly parse the CLI `MAXIMUM` field and its occurrence time, track preliminary versus later report versions, and leave the market unresolved until the final applicable revision state is observed.
8. Forecast Calibration Engine produces a discrete probability mass function over integer settlement temperatures using provider normalization, lead-time weighting, conditional bias correction, and uncertainty inflation.
9. Nowcast Bridge estimates current station state between official observations and produces lag-aware current temperature, uncertainty, near-term trend, shock risk, and confidence downgrade fields.
10. Path Progress Engine conditions the settlement distribution on current high-so-far, threshold gap, remaining effective heating or cooling window, expected slope, and late-day convergence decay.
11. Microstructure Engine converts current orderbook state into executable prices and fill realism metrics by side, size, and time-to-close bucket.
12. EV Engine computes raw edge, frictions, haircuts, executable EV, and candidate actions for BUY_YES, BUY_NO, REDUCE, EXIT, or NO_ACTION.
13. Risk and Governance Engine applies hard halts, exposure rules, city qualification state, correlation controls, stale-data rules, and manual kill switches.
14. Decision Orchestrator chooses a final action, writes a StrategyDecisionExplanation JSON object, and routes that action to replay, shadow, or live adapter depending on RunMode.
15. Shadow Execution Engine records hypothetical fills, marks positions, and reconciles realized outcomes after market settlement.
16. Replay Engine reruns the exact event stream as-of historical time, regenerates decisions, and produces qualification reports.
17. Drift and Monitoring services track calibration, nowcast error, parser discrepancies, market ingestion stability, fill quality drift, and explanation schema drift.
18. Feedback jobs update uncertainty haircuts, city qualification state, regime qualification, maker viability, taker viability, and provisional threshold calibrations using settled replay and shadow evidence.
19. Thin Live Adapter remains disabled until all live gates pass. When enabled, it only translates approved ExecutionPlans into orders and never contains model logic.

Architectural shape:

- Control plane: reference truth, configs, parser registry, qualification state, risk limits, schema versions.
- Data plane: append-only raw stores, normalized stores, feature tables, decision logs, replay artifacts.
- Decision plane: forecast, nowcast, path, microstructure, EV, risk, orchestration.
- Feedback plane: replay analytics, shadow reconciliation, drift monitoring, threshold recalibration.

## 5. Detailed Module Specifications

### 5.1 Universe Selector

| Field | Specification |
| --- | --- |
| Purpose | Restrict the system to approved city series and valid market types only. |
| Inputs | SeriesDefinition, MarketDefinition, CityQualificationState, RunMode. |
| Outputs | Approved market universe for current run. |
| Internal responsibilities | Filter to max three city series, binary single-threshold daily high markets, active or near-active markets, one open market per city. |
| Invariants | No auto-expansion to new cities in MVP. No multivariate, range, or spread markets. |
| Failure cases | Missing city mapping, ambiguous market rules, provisional market without rule validation, duplicate active market for same city-date-threshold. |
| Separation reason | Strategy logic must never discover or qualify markets on its own. Universe control is governance, not alpha. |

### 5.2 Reference Registry

| Field | Specification |
| --- | --- |
| Purpose | Hold authoritative definitions for markets, stations, city behavior, fee versions, and parser versions. |
| Inputs | Market metadata, manual configuration, validation outputs. |
| Outputs | Resolved reference objects used by all downstream modules. |
| Internal responsibilities | Version station mappings, city thermal profiles, onshore wind sectors, peak heating windows, fee schedule version, and schema versions. |
| Invariants | Reference rows are versioned, immutable once activated, and point to provenance. |
| Failure cases | Conflicting station assignment, missing timezone, unresolved DST semantics, missing source locator. |
| Separation reason | Reference truth changes rarely and should not be mixed with volatile market or weather data. |

### 5.3 Settlement Rule Resolver

| Field | Specification |
| --- | --- |
| Purpose | Convert market text and verification source metadata into the exact settlement contract. |
| Inputs | MarketDefinition, rules_primary, rules_secondary, outcome verification locator, StationReference. |
| Outputs | SettlementRule. |
| Internal responsibilities | Parse threshold, operator, station, source product, reporting window, local standard time semantics, CLI `MAXIMUM` field semantics, report versioning, revision-monitor policy, and ambiguity flags. |
| Invariants | Every tradable market must have exactly one validated SettlementRule. If not, the market is blocked. |
| Failure cases | Rule text inconsistent with source, unclear station, unclear inclusive vs exclusive threshold, unresolved DST handling, parser mismatch with historical outcomes. |
| Separation reason | Settlement semantics are the highest-risk truth surface and must be isolated from forecasting code. |

### 5.4 Ingestion Hub

| Field | Specification |
| --- | --- |
| Purpose | Collect and persist raw weather and market payloads. |
| Inputs | External APIs, WebSocket streams, scheduled pulls. |
| Outputs | Append-only raw payload records with provenance metadata. |
| Internal responsibilities | Scheduling, retries, backoff, checksum capture, parser version stamping, stale-source detection, and reconnect handling. |
| Invariants | Raw payloads are never overwritten. Normalization never mutates raw payloads. |
| Failure cases | Rate limits, WebSocket sequence gaps, malformed source payloads, timeouts, source outages. |
| Separation reason | Data capture must remain independent from parser behavior and modeling logic. |

### 5.5 Normalization Layer

| Field | Specification |
| --- | --- |
| Purpose | Convert heterogeneous raw payloads into canonical records. |
| Inputs | Raw weather and market payloads, parser definitions. |
| Outputs | Normalized ObservationSnapshot, ForecastSnapshot, OrderbookSnapshot, MarketSnapshot, TradeSnapshot records. |
| Internal responsibilities | Unit normalization, timestamp normalization, station mapping, price-grid normalization, event sequencing, quality flags. |
| Invariants | Normalized records preserve provenance links to raw payload ids. |
| Failure cases | Parser version mismatch, impossible unit conversion, sequence discontinuity, source field drift. |
| Separation reason | Canonicalization must be stable even while models and thresholds evolve. |

### 5.6 Forecast Calibration Engine

| Field | Specification |
| --- | --- |
| Purpose | Estimate a settlement-temperature probability distribution before path conditioning. |
| Inputs | ForecastSnapshot records, CityProfile, provider reliability stats, conditional bias tables. |
| Outputs | ForecastDistribution and provider-level diagnostics. |
| Internal responsibilities | Provider normalization, lead-time weighting, conditional bias correction, uncertainty inflation, threshold PMF generation. |
| Invariants | Output is a discrete PMF over integer settlement temperatures that sums to 1. |
| Failure cases | Missing required providers, provider disagreement beyond halt thresholds, insufficient historical sample for calibration without fallback bucket. |
| Separation reason | Provider fusion and calibration must be independent from current-state bridging and market execution logic. |

### 5.7 Nowcast Bridge

| Field | Specification |
| --- | --- |
| Purpose | Estimate current station state between official observations. |
| Inputs | Latest official observations, recent observation history, HRRR short-horizon fields, weather codes, radar or MRMS indicators, city profile. |
| Outputs | CurrentStateEstimate. |
| Internal responsibilities | Baseline interpolation or extrapolation, observation-lag penalty, shock detection, non-linear cloud-cover shock adjustment, confidence downgrade. |
| Invariants | This module outputs only current-state estimates and near-term trend, not settlement probability. |
| Failure cases | Observation gap too large, shock evidence contradictory, impossible trend, stale upstream data. |
| Separation reason | Bridging the current state is a different problem from estimating the final daily high. Mixing them hides error sources. |

### 5.8 Path Progress Engine

| Field | Specification |
| --- | --- |
| Purpose | Transform an unconditional settlement distribution into a path-conditioned distribution. |
| Inputs | ForecastDistribution, CurrentStateEstimate, observation history, CityProfile, SettlementRule. |
| Outputs | PathProgressState and path-adjusted probability distribution. |
| Internal responsibilities | Compute high so far, threshold gap, residual heating or cooling opportunity, reachability score, late-day decay, and path uncertainty add-on. |
| Invariants | If the threshold is already irrevocably crossed by official state, YES probability must collapse toward 1 minus revision penalty. |
| Failure cases | Current high unavailable, invalid local heating window, contradictory state estimate, threshold rule unresolved. |
| Separation reason | Path dependence should be auditable as its own transform, not buried inside provider fusion. |

### 5.9 Microstructure Engine

| Field | Specification |
| --- | --- |
| Purpose | Convert displayed market state into executable price and fill realism metrics. |
| Inputs | OrderbookSnapshot, trade stream, market ticker stream, time-to-close, target size, fee schedule version. |
| Outputs | MicrostructureAssessment and TradabilityAssessment. |
| Internal responsibilities | Compute depth-adjusted taker prices, maker queue prospects, quote durability, ghost liquidity, slippage, adverse selection, and tradability score. |
| Invariants | Executable price must be size-aware. Top-of-book alone is never sufficient. |
| Failure cases | Sequence gap, stale book, missing recent trades, invalid price grid, crossed or impossible book reconstruction. |
| Separation reason | Execution realism should evolve independently from weather modeling and support replay calibration directly. |

### 5.10 EV Engine

| Field | Specification |
| --- | --- |
| Purpose | Convert path-aware probabilities and executable prices into candidate action economics. |
| Inputs | Path-adjusted probability, MicrostructureAssessment, RegimeAssessment, CityQualificationState, RiskDecision context. |
| Outputs | EdgeEstimate and candidate ExecutionPlans. |
| Internal responsibilities | Compute raw edge, fee cost, slippage cost, adverse selection penalty, friction-to-edge ratio, haircuts, and executable EV. |
| Invariants | No candidate plan is valid unless executable EV is positive after all active haircuts and no hard veto is present. |
| Failure cases | Direction mismatch, negative raw edge, undefined fee schedule, missing friction estimate. |
| Separation reason | Alpha and friction accounting must be explicit and testable. |

### 5.11 Risk and Governance Engine

| Field | Specification |
| --- | --- |
| Purpose | Apply hard limits, halts, kill switches, and portfolio controls. |
| Inputs | EdgeEstimate, PositionState, Shadow metrics, source health, CityQualificationState, KillSwitchState. |
| Outputs | RiskDecision. |
| Internal responsibilities | Enforce max exposure, stale-data halts, thin-book halts, provider disagreement halts, settlement ambiguity halts, and manual overrides. |
| Invariants | Hard halts override EV. Qualification state overrides temptation. |
| Failure cases | Invalid limit config, stale qualification state, manual kill switch unresolved. |
| Separation reason | Risk policy is governance and must not be hidden inside trade scoring. |

### 5.12 Decision Orchestrator

| Field | Specification |
| --- | --- |
| Purpose | Produce exactly one final decision object per evaluation cycle. |
| Inputs | All prior module outputs plus RunMode. |
| Outputs | StrategyDecisionExplanation and optional ExecutionPlan dispatch. |
| Internal responsibilities | Decision state selection, explanation generation, idempotency, persistence, and routing to replay, shadow, or live adapter. |
| Invariants | Every evaluation emits a decision, even if the decision is NO_TRADE or HALT. |
| Failure cases | Missing upstream objects, schema serialization failure, duplicate decision id, clock skew. |
| Separation reason | Execution routing and explanation emission must be centralized so replay and live remain behaviorally identical. |

### 5.13 Replay and Shadow Feedback Layer

| Field | Specification |
| --- | --- |
| Purpose | Validate the system and recalibrate provisional controls. |
| Inputs | Raw historical data, normalized data, decisions, settled outcomes, shadow fills. |
| Outputs | Qualification reports, recalibrated haircuts, threshold candidates, drift reports. |
| Internal responsibilities | True as-of replay, shadow reconciliation, attribution, confidence intervals, regime performance, and city promotion or demotion updates. |
| Invariants | Incentives are fixed at zero. Only settled evidence can update qualification states. |
| Failure cases | Missing as-of timestamps, incomplete orderbook history, unresolved settlement mismatch. |
| Separation reason | Validation must be independent from the live decision loop and rerunnable from preserved artifacts. |

## 6. Core Domain Model

### 6.1 SeriesDefinition

| Attribute | Specification |
| --- | --- |
| Meaning | Defines a Kalshi series such as "highest temperature in NYC today". |
| Essential fields | series_ticker, title, city_id, category, frequency, active_from, active_to, source_provenance. |
| Mutability | Immutable by version. New version for metadata changes. |
| Invariants | Maps to exactly one city_id in MVP. |
| Lifecycle role | Root object for city market discovery. |

### 6.2 MarketDefinition

| Attribute | Specification |
| --- | --- |
| Meaning | Defines one tradable binary threshold market. |
| Essential fields | market_ticker, event_ticker, series_ticker, market_type, threshold_f, operator, open_time, close_time, settlement_ts, rules_primary, rules_secondary, price_level_structure, price_ranges, can_close_early, is_provisional. |
| Mutability | Mutable while open for status and timing fields, immutable by snapshot in replay. |
| Invariants | Must be binary and single-threshold in MVP. |
| Lifecycle role | Tradable object consumed by decision engine. |

### 6.3 SettlementRule

| Attribute | Specification |
| --- | --- |
| Meaning | Exact settlement contract derived from rules and verification source. For MVP daily high-temperature markets, the expected source_kind is the market's official verification source resolving to the final NWS Daily Climate Report, but this must still be validated per market rather than assumed globally. |
| Essential fields | settlement_rule_id, market_ticker, settlement_variable, operator, threshold_f, inclusive_flag, station_id, source_kind, source_locator, local_standard_window_start, local_standard_window_end, parser_version, validation_status, ambiguity_flags. |
| Mutability | Immutable by version once activated. |
| Invariants | One validated rule per market. No proxy source allowed. |
| Lifecycle role | Settlement truth anchor used by parsing, replay, and decision modules. |

### 6.4 StationReference

| Attribute | Specification |
| --- | --- |
| Meaning | Canonical weather station mapping for settlement and observations. |
| Essential fields | station_id, station_name, metar_code, nws_station_api_id, climate_product_id, latitude, longitude, timezone, wfo_office, grid_x, grid_y, climate_timezone_basis, source_urls. |
| Mutability | Immutable by version. |
| Invariants | Timezone and climate reporting basis must be explicit. |
| Lifecycle role | Resolves all station-level weather data sources. |

### 6.5 SettlementReportSnapshot

| Attribute | Specification |
| --- | --- |
| Meaning | Canonical parsed climate-report record used for settlement validation and revision monitoring. |
| Essential fields | station_id, climate_product_id, issue_time, report_version, report_status, local_standard_window_start, local_standard_window_end, max_temp_f, max_temp_time_local, min_temp_f, raw_text_payload_id, parser_version, revision_flags, source_url. |
| Mutability | Immutable per report version. |
| Invariants | Parsed from the official climate report text; later report versions never overwrite earlier versions. |
| Lifecycle role | Tracks preliminary and final settlement-source revisions and feeds settlement reconciliation. |

### 6.6 CityProfile

| Attribute | Specification |
| --- | --- |
| Meaning | City-specific thermal and microclimate behavior used by calibration and nowcast logic. |
| Essential fields | city_id, display_name, station_id, region_cluster, marine_sensitive_flag, onshore_wind_sectors, typical_peak_hour_local_by_season, heating_window_by_season, cloud_shock_cap_f, storm_shock_cap_f, marine_intrusion_cap_f, wind_shift_cap_f, calibration_buckets_version. |
| Mutability | Mutable by version only. |
| Invariants | Exactly one active profile per city. |
| Lifecycle role | Parameter surface for conditional bias and shock-aware nowcasting. |

### 6.7 ObservationSnapshot

| Attribute | Specification |
| --- | --- |
| Meaning | Canonical official observation record. |
| Essential fields | station_id, event_time, ingest_time, temperature_f, dewpoint_f, wind_dir_deg, wind_speed_kt, sky_cover_code, ceiling_ft, visibility_mi, weather_codes, quality_flags, source_payload_id. |
| Mutability | Immutable. |
| Invariants | event_time and ingest_time are always preserved separately. |
| Lifecycle role | Primary input to nowcast bridge and high-so-far tracking. |

### 6.8 CurrentStateEstimate

| Attribute | Specification |
| --- | --- |
| Meaning | Model estimate of the current station state between official observations. |
| Essential fields | as_of_time, station_id, current_temp_est_f, current_temp_sigma_f, near_term_slope_f_per_hr, observation_lag_minutes, observation_lag_penalty, shock_risk_score, cloud_cover_shock_adjustment_f, storm_shock_adjustment_f, marine_intrusion_adjustment_f, wind_shift_adjustment_f, discontinuity_suspected, confidence_downgrade, provenance_refs. |
| Mutability | Immutable per evaluation cycle. |
| Invariants | Represents current state only, not final outcome. |
| Lifecycle role | Feeds path-conditioning and uncertainty controls. |

### 6.9 ForecastSnapshot

| Attribute | Specification |
| --- | --- |
| Meaning | Normalized single-provider forecast state for one issuance. |
| Essential fields | provider_id, provider_run_time, ingest_time, valid_for_times, hourly_temp_path_f, cloud_cover_path_pct, wind_path, precipitation_path, provider_metadata, source_payload_id. |
| Mutability | Immutable. |
| Invariants | Provider run time and ingest time are distinct. |
| Lifecycle role | Atomic unit of provider fusion and replay. |

### 6.10 ForecastDistribution

| Attribute | Specification |
| --- | --- |
| Meaning | Discrete PMF over settlement temperatures before and after path conditioning. |
| Essential fields | as_of_time, station_id, support_temps_f, pmf, provider_weights, base_entropy, sigma_equivalent_f, calibration_version, conditioned_flag. |
| Mutability | Immutable per evaluation cycle. |
| Invariants | PMF sums to 1 across integer Fahrenheit support. |
| Lifecycle role | Main probabilistic input to EV engine. |

### 6.11 PathProgressState

| Attribute | Specification |
| --- | --- |
| Meaning | Intraday path state relative to threshold and remaining opportunity. |
| Essential fields | as_of_time, current_high_so_far_f, current_temp_f, threshold_gap_f, remaining_effective_window_minutes, estimated_intraday_slope_f_per_hr, solar_insolation_vector, thermal_ceiling_estimate_f, residual_gain_mean_f, residual_gain_p80_f, reachability_score, late_day_decay_factor, path_uncertainty_addon, threshold_already_crossed_flag. |
| Mutability | Immutable per evaluation cycle. |
| Invariants | If threshold_already_crossed_flag is true for a monotone daily-high rule, reachability_score is 1. |
| Lifecycle role | Converts unconditional probabilities into path-aware probabilities or blocks. |

### 6.12 MarketSnapshot

| Attribute | Specification |
| --- | --- |
| Meaning | Canonical snapshot of Kalshi market metadata and top-level fields. |
| Essential fields | market_ticker, status, open_time, close_time, settlement_ts, yes_bid_dollars, yes_ask_dollars, no_bid_dollars, no_ask_dollars, yes_bid_size_fp, yes_ask_size_fp, no_bid_size_fp, no_ask_size_fp, last_price_dollars, last_trade_size_fp, volume_fp, open_interest_fp, updated_time, source_payload_id. |
| Mutability | Mutable while market is live; immutable by snapshot in replay. |
| Invariants | Price fields must respect price grid. |
| Lifecycle role | Entry point to market executability analysis. |

### 6.13 OrderbookSnapshot

| Attribute | Specification |
| --- | --- |
| Meaning | Reconstructed aggregated orderbook state at one sequence point. |
| Essential fields | market_ticker, as_of_time, seq, yes_bids_ladder, no_bids_ladder, implied_yes_asks_ladder, implied_no_asks_ladder, checksum_status, source_refs. |
| Mutability | Immutable once emitted. |
| Invariants | Ladder must be sequence-consistent and price-grid-valid. |
| Lifecycle role | Used by taker and maker execution simulation. |

### 6.14 MicrostructureAssessment

| Attribute | Specification |
| --- | --- |
| Meaning | Quantified execution realism for a side-size candidate. |
| Essential fields | market_ticker, side, quantity_fp, top_of_book_price, executable_wap_price, depth_consumed_levels, slippage_cost, top_of_book_durability_score, quote_stability_score, ghost_liquidity_ratio, maker_fill_probability, maker_adverse_selection_penalty, taker_adverse_selection_penalty, time_to_close_bucket. |
| Mutability | Immutable per evaluation cycle and candidate size. |
| Invariants | Executable_wap_price must be computed from effective depth, not displayed depth alone. |
| Lifecycle role | Feeds EV and tradability scoring. |

### 6.15 TradabilityAssessment

| Attribute | Specification |
| --- | --- |
| Meaning | Summary judgment on whether the market is practically tradable at target size. |
| Essential fields | market_ticker, side, tradability_score, thin_book_flag, stale_book_flag, friction_overload_flag, allowed_taker_flag, allowed_maker_flag, block_reasons. |
| Mutability | Immutable per evaluation cycle. |
| Invariants | Any hard block sets tradability_score below minimum threshold. |
| Lifecycle role | Direct filter before EV can produce actionable plans. |

### 6.16 EdgeEstimate

| Attribute | Specification |
| --- | --- |
| Meaning | Economics of a candidate trade direction and size. |
| Essential fields | market_ticker, side, quantity_fp, p_model, p_market_exec, raw_edge, fee_cost, slippage_cost, adverse_selection_penalty, total_friction, friction_to_edge_ratio, uncertainty_haircut, regime_haircut, portfolio_haircut, edge_conf_adj, executable_ev_per_contract, executable_ev_total. |
| Mutability | Immutable per candidate. |
| Invariants | total_friction equals fee_cost plus slippage_cost plus adverse_selection_penalty. |
| Lifecycle role | Core decision input. |

### 6.17 ExecutionPlan

| Attribute | Specification |
| --- | --- |
| Meaning | Concrete order intention produced after EV and risk checks. |
| Essential fields | plan_id, action_type, market_ticker, side, quantity_fp, order_type, limit_price_dollars, time_in_force, max_cost_dollars, cancel_on_pause, maker_flag, rationale_codes, expires_at, run_mode. |
| Mutability | Immutable after emission. |
| Invariants | No live ExecutionPlan may exist without a positive RiskDecision. |
| Lifecycle role | Only object the live adapter may translate into an API order. |

### 6.18 RegimeAssessment

| Attribute | Specification |
| --- | --- |
| Meaning | Current measurable weather or execution regime affecting edge validity. |
| Essential fields | city_id, as_of_time, active_regime, regime_scores, feature_values, block_flag, haircut_value, sizing_multiplier, explanation_codes. |
| Mutability | Immutable per evaluation cycle. |
| Invariants | Only one primary regime may be active in MVP, with optional secondary flags for diagnostics. |
| Lifecycle role | Applies regime-linked block or haircut behavior. |

### 6.19 RiskDecision

| Attribute | Specification |
| --- | --- |
| Meaning | Final governance result for a candidate action. |
| Essential fields | risk_state, hard_halt_flag, city_halt_flag, global_halt_flag, exposure_ok, correlation_ok, source_health_ok, qualification_ok, veto_reasons, manual_override_state. |
| Mutability | Immutable per evaluation cycle. |
| Invariants | Hard veto reasons are explicit and ordered by precedence. |
| Lifecycle role | Governs whether an ExecutionPlan may exist. |

### 6.20 StrategyDecisionExplanation

| Attribute | Specification |
| --- | --- |
| Meaning | Versioned machine-readable explanation for the final decision. |
| Essential fields | schema_version, decision_id, run_mode, as_of_time, market_ticker, city_id, station_id, settlement_rule_id, data_freshness, current_state, forecast_summary, path_state, microstructure_summary, edge_summary, regime_summary, risk_summary, final_decision, explanation_codes, provenance_refs, module_versions. |
| Mutability | Immutable. |
| Invariants | schema_version is mandatory and semver-like, for example `1.0.0`. |
| Lifecycle role | Stable audit and replay artifact for debugging, drift detection, and qualification. |

### 6.20.1 Frozen StrategyDecisionExplanation Schema v1.0.0

This schema must be frozen and checked into the repo before Phase 1 is considered complete.

| Field | Type |
| --- | --- |
| schema_version | string |
| decision_id | string |
| run_mode | enum(`REPLAY`,`SHADOW`,`LIVE_READONLY`,`LIVE_TRADE`) |
| as_of_time | timestamp |
| market_ticker | string |
| city_id | string |
| station_id | string |
| settlement_rule_id | string |
| data_freshness | object |
| current_state | object |
| forecast_summary | object |
| path_state | object |
| microstructure_summary | object |
| edge_summary | object |
| regime_summary | object |
| risk_summary | object |
| final_decision | string |
| explanation_codes | array<string> |
| provenance_refs | array<string> |
| module_versions | object<string,string> |

### 6.21 PositionState

| Attribute | Specification |
| --- | --- |
| Meaning | Current real or shadow position for one market. |
| Essential fields | market_ticker, side, quantity_fp, avg_entry_price_dollars, fees_paid_dollars, realized_pnl_dollars, unrealized_mark_dollars, opened_at, last_updated_at, source. |
| Mutability | Mutable. |
| Invariants | Only one open position per city in MVP. |
| Lifecycle role | Input to REDUCE, EXIT, and correlation controls. |

### 6.22 ShadowFill

| Attribute | Specification |
| --- | --- |
| Meaning | Hypothetical execution record in shadow mode. |
| Essential fields | shadow_fill_id, decision_id, market_ticker, side, quantity_fp, modeled_fill_price_dollars, fill_scenario, fill_confidence, modeled_fee_dollars, modeled_slippage_dollars, modeled_adverse_selection_dollars, fill_time, reconciliation_status. |
| Mutability | Mutable until reconciled after settlement. |
| Invariants | Linked to one StrategyDecisionExplanation. |
| Lifecycle role | Bridge between decisions and shadow performance evaluation. |

### 6.23 ShadowPosition

| Attribute | Specification |
| --- | --- |
| Meaning | Position ledger for simulated trading. |
| Essential fields | city_id, market_ticker, open_quantity_fp, avg_cost_dollars, cumulative_fees_dollars, mark_pnl_dollars, settled_pnl_dollars, lifecycle_status. |
| Mutability | Mutable. |
| Invariants | One per city in MVP. |
| Lifecycle role | Used for shadow PnL, drawdown, and qualification statistics. |

### 6.24 CityQualificationState

| Attribute | Specification |
| --- | --- |
| Meaning | Empirical state machine for whether a city is eligible for observe, shadow, or live use. |
| Essential fields | city_id, state, effective_from, effective_to, settlement_validation_score, calibration_score, nowcast_score, path_score, market_depth_score, slippage_score, shadow_ev_score, drawdown_score, promotion_reasons, demotion_reasons. |
| Mutability | Mutable as state machine. |
| Invariants | Live use is impossible unless state is LIVE_ELIGIBLE or LIVE_PILOT. |
| Lifecycle role | City-level governance gate. |

### 6.25 KillSwitchState

| Attribute | Specification |
| --- | --- |
| Meaning | Persistent halt state for global or city-level shutdown. |
| Essential fields | scope, state, triggered_at, trigger_reason, cleared_at, cleared_by, sticky_flag. |
| Mutability | Mutable under audit. |
| Invariants | Global halt overrides city halt; sticky halts require manual clear. |
| Lifecycle role | Highest-precedence governance control. |

### 6.26 RunMode

| Attribute | Specification |
| --- | --- |
| Meaning | Execution environment for the decision system. |
| Essential fields | Enum values `REPLAY`, `SHADOW`, `LIVE_READONLY`, `LIVE_TRADE`. |
| Mutability | Immutable per run. |
| Invariants | `LIVE_TRADE` is disallowed until live gating is passed. |
| Lifecycle role | Selects sink behavior without changing model logic. |

## 7. Data Architecture

Storage design for MVP:

- Reference truth store: PostgreSQL tables for SeriesDefinition, MarketDefinition, SettlementRule, StationReference, CityProfile, CityQualificationState, KillSwitchState, fee schedule versions, and parser registry.
- Raw weather store: append-only compressed files in local object layout such as `data/raw/weather/{source}/{date}/...`, preserving exact payloads including climate report text products.
- Normalized weather store: partitioned Parquet with DuckDB query layer for observations, forecasts, SettlementReportSnapshot records, and derived hourly paths.
- Raw market store: append-only WebSocket packet logs and REST snapshots under `data/raw/market/{channel}/{date}/...`.
- Derived analytics store: PostgreSQL and DuckDB tables for CurrentStateEstimate, ForecastDistribution, PathProgressState, MicrostructureAssessment, EdgeEstimate, and StrategyDecisionExplanation.
- Replay artifact store: run-scoped immutable folders such as `data/replay_runs/{run_id}/...` containing config hash, input slices, outputs, metrics, and report JSON.
- Shadow and live audit store: PostgreSQL tables plus append-only JSONL audit logs for decisions, shadow fills, live orders, fills, cancellations, and reconciliations.

Timestamp semantics:

- `event_time`: when the source says the underlying event happened.
- `ingest_time`: when this system received the payload.
- `as_of_time`: the decision-system clock time for what was knowable.
- `valid_for_time`: the future time a forecast value applies to.

Rules:

- Replay must reconstruct state using only records with `ingest_time <= as_of_time`.
- Forecasts must preserve provider run time and `valid_for_time` separately.
- Market and orderbook reconstruction must preserve source sequencing and `as_of_time`.
- The system must track `ingest_time - event_time` drift by source. If that drift rises materially versus source baseline, uncertainty widens automatically and may trigger source-health halts.

Why raw versus normalized versus derived separation matters:

- Raw data preserves legal and scientific auditability.
- Normalized data prevents source quirks from leaking into model code.
- Derived data changes often and should not rewrite truth.
- Replay depends on being able to rerun new parsers or models against the same raw capture.
- Settlement disputes and parser drift cannot be debugged if raw payloads are lost.

## 8. Ingestion Architecture

### 8.1 Adapter rules common to all sources

- Every adapter writes raw payload first, then normalized rows.
- Every raw record stores source name, endpoint or channel, request parameters, HTTP status or WebSocket metadata, payload hash, parser version, event_time if present, and ingest_time.
- Retries use exponential backoff with jitter.
- Repeated parser failures raise a source-health alert and halt normalization for that source until reviewed.
- Stale-source behavior is explicit: soft stale inflates uncertainty and blocks new maker plans; hard stale halts new trades and may trigger CANCEL_PENDING on resting orders.

### 8.2 Adapter matrix

| Adapter | Pull or push | Cadence or stream | Freshness SLA | Raw storage | Normalized storage | Parser versioning | Provenance | Retry and backoff | Stale behavior | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Official observations via NWS station API | Scheduled pull | Every 2 minutes per active city | Soft stale at 12 minutes, hard stale at 25 minutes | Raw JSON | ObservationSnapshot | Versioned per endpoint schema | station_id, endpoint, headers, payload hash | 3 retries over 30 seconds | Soft stale adds lag penalty; hard stale blocks new trades | Decoded METAR pull for the same station |
| Decoded METAR reference pull | Scheduled pull | Best-effort between hh:10 and hh:15 local processing guidance | Soft stale at 20 minutes, hard stale at 40 minutes | Raw text | ObservationSnapshot with lower trust flag | Versioned text parser | metar_code, file path, payload hash | 3 retries over 5 minutes | Cross-check only; if primary obs missing and METAR available, use with confidence downgrade | None |
| Official climate report (CLI) settlement source | Scheduled pull | Every 15 minutes from 3:00 PM to 6:00 PM local and every 15 minutes from 12:15 AM to 5:15 AM local until final version is stable | Soft stale at 60 minutes during active monitoring, hard stale at market settlement plus 3 hours if final source unresolved | Raw text or HTML-derived text | SettlementReportSnapshot | Versioned text parser keyed by climate product format | climate_product_id, version, issue time, payload hash | 5 retries over 30 minutes | Market remains unresolved; no settlement trust and no parser validation advance | Manual reconciliation only |
| Official forecast via NWS `/points` and hourly forecast | Scheduled pull with periodic remap | `/points` daily and on startup, hourly forecast every 15 minutes or ETag change | Soft stale at 90 minutes, hard stale at 4 hours | Raw JSON | ForecastSnapshot | Versioned per API schema and feature flags | office, grid, ETag, payload hash | 3 retries over 5 minutes | Soft stale inflates uncertainty; hard stale blocks new entries if no alternate forecast | Keep last valid forecast for diagnostics only |
| Deterministic short-range model: HRRR surface fields | Scheduled pull | Check for new cycle every 20 minutes; ingest latest available run | Soft stale at 2 hours from expected run availability, hard stale at 4 hours | Raw GRIB subset or equivalent raw payload | ForecastSnapshot | Versioned field-extractor definition | run_time, forecast_hour, grid coords, payload hash | 5 retries over 20 minutes | Soft stale lowers provider weight; hard stale blocks regimes requiring short-term path detail | NWS hourly forecast only, with larger uncertainty floor |
| Ensemble model: GEFS | Scheduled pull | 4 cycles daily; ingest each new cycle and relevant lead hours | Soft stale at 8 hours, hard stale at 14 hours | Raw GRIB subset or equivalent raw payload | ForecastSnapshot | Versioned field-extractor definition | run_time, member id, lead hour, payload hash | 5 retries over 60 minutes | Soft stale lowers ensemble weight; hard stale removes ensemble contribution and inflates disagreement penalty | Use deterministic-only distribution with uncertainty inflation |
| Lightweight radar or MRMS reflectivity proxy | Scheduled pull | Every 2 minutes for active cities with small upwind geographic query only | Soft stale at 10 minutes, hard stale at 20 minutes | Raw JSON or image-derived summary artifact | Shock proxy record referenced by CurrentStateEstimate provenance | Versioned extractor definition | city_id, proxy source, query box, payload hash | 3 retries over 10 minutes | Missing proxy downgrades shock confidence but does not hard-halt by itself | Continue with observation and model-only shock detection |
| Market metadata via Kalshi REST | Scheduled pull | Every 60 seconds for active series, plus startup full sync | Soft stale at 2 minutes, hard stale at 10 minutes | Raw JSON | MarketSnapshot, MarketDefinition updates | Versioned per Kalshi API version | endpoint, query params, payload hash | 3 retries over 30 seconds | Soft stale blocks new maker plans; hard stale halts city trading | Last good metadata for display only |
| Market lifecycle via Kalshi WebSocket | Push stream | Continuous | Soft stale at 10 seconds, hard stale at 60 seconds | Raw WebSocket frames | Market lifecycle events | Versioned channel parser | channel, sid, seq, payload hash | Reconnect with backoff 1s, 2s, 5s, 10s | Missing lifecycle forces metadata resync and may halt affected markets | REST resync |
| Orderbook state via Kalshi WebSocket plus REST snapshot | Push plus snapshot | Continuous deltas, snapshot on subscribe or gap | Soft stale at 3 seconds, hard stale at 15 seconds | Raw frames plus raw snapshot payloads | OrderbookSnapshot | Versioned channel parser | market_ticker, sid, seq, payload hashes | Immediate resubscribe on gap; full snapshot rebuild on seq discontinuity | Soft stale blocks maker plans; hard stale blocks all new trades and cancels pending | REST snapshot only, marked low confidence |
| Public trades via Kalshi WebSocket | Push stream | Continuous | Soft stale at 5 seconds, hard stale at 30 seconds | Raw WebSocket frames | TradeSnapshot | Versioned channel parser | market_ticker, sid, payload hash | Reconnect with rolling backoff | Missing trades degrades ghost-liquidity and durability estimates | REST trade backfill if available within live window |
| Historical replay data via Kalshi historical endpoints and local archives | Scheduled pull and offline batch | Daily archival and on-demand backfill | Not applicable to live health | Raw JSON and archived packets | Replay inputs | Versioned import manifest | endpoint, cutoff timestamps, payload hash | Batched retries with cursor resume | Missing historical depth prevents qualifying microstructure replay | Replay-light mode only |

Health checks:

- Adapter heartbeat.
- Recent success rate.
- Parser error rate.
- Freshness percent within SLA.
- Sequence-gap count for streaming sources.
- Source-schema hash drift.

### 8.3 Verified 2026 Kalshi realities and startup exchange capability check

The design now requires a startup exchange capability check before coding and at every production startup.

Verified against official Kalshi docs and public endpoints as of 2026-04-05:

- The authenticated orderbook WebSocket channel sends `orderbook_snapshot` first and then `orderbook_delta` updates with `sid`, `seq`, `market_ticker`, `market_id`, `price_dollars`, `delta_fp`, `side`, and `ts`.
- Historical endpoints exist and are mandatory for older data: `/historical/cutoff`, `/historical/markets`, `/historical/markets/{ticker}`, `/historical/markets/{ticker}/candlesticks`, `/historical/trades`, `/historical/fills`, and `/historical/orders`.
- The public historical cutoff endpoint returned `market_settled_ts = 2026-01-05T00:00:00Z`, `trades_created_ts = 2026-01-05T00:00:00Z`, and `orders_updated_ts = 2026-01-05T00:00:00Z` on 2026-04-05.
- Public weather series metadata for `KXHIGHNY` returned `fee_type = quadratic` and `fee_multiplier = 1` at the series level on 2026-04-05.
- `GET /series/fee_changes?series_ticker=KXHIGHNY&show_historical=true` returned no scheduled fee changes on 2026-04-05.

Startup exchange capability check requirements:

- Re-verify the orderbook channel schema and reconnect behavior.
- Query historical cutoffs and persist them in the reference store with check timestamp.
- Query active weather series metadata and fee-change endpoints; do not assume market-level fee fields are populated.
- Verify fee rounding documentation version and active fee schedule version hash.
- Fail startup into `LIVE_READONLY` or `SHADOW` only if any of these checks are unresolved.

## 9. Forecast / Calibration Architecture

### 9.1 Fixed MVP design choices

- Provider set in MVP:
  - Official forecast: NWS hourly forecast.
  - Deterministic short-range model: HRRR.
  - Ensemble: GEFS.
- Forecast target domain: integer settlement temperatures in Fahrenheit.
- Output type: discrete PMF, not just point estimate and sigma.
- Fusion seed weights are lead-time dependent and deterministic until enough city-specific evidence exists.

Seed weights by time before the city's modeled peak hour:

| Lead bucket | NWS weight | HRRR weight | GEFS weight |
| --- | --- | --- | --- |
| More than 12 hours | 0.35 | 0.20 | 0.45 |
| 6 to 12 hours | 0.35 | 0.40 | 0.25 |
| Less than 6 hours | 0.25 | 0.60 | 0.15 |

These are fixed MVP defaults. They are provisional and must be replaced by reliability-weighted evidence before live eligibility.

### 9.2 Provider normalization

Per provider and issuance:

1. Convert provider path into station-local hourly path on the settlement clock.
2. Resolve unit differences and station-grid mappings.
3. Derive an implied daily-high path consistent with the settlement window, including local standard time boundary handling.
4. Convert provider output into a per-provider daily-high distribution:
   - Deterministic providers use the implied max plus calibrated error distribution.
   - Ensemble providers use member maxima plus kernel smoothing and calibrated dispersion.

### 9.3 Provider reliability tracking

Reliability is tracked by provider, city, lead bucket, and season on settled days using:

- Daily-high MAE.
- Discrete log loss on actual settlement temperature bin.
- Threshold Brier score on traded thresholds.
- Calibration slope and intercept on threshold probabilities.

MVP update rule:

- Use seed weights until a provider-city-lead bucket has at least 60 settled days.
- After that, compute relative weight as inverse rolling CRPS proxy with exponential decay half-life 30 settled days.
- Clamp weight multipliers to the range [0.5, 1.5] to avoid regime overreaction.

### 9.4 Conditional bias correction

Bias correction is not a constant offset. It is a hierarchical lookup with backoff.

Primary conditioning dimensions:

- Wind sector: calm plus 8 directional bins.
- Cloud bucket: clear below 30 percent, mixed 30 to 70 percent, overcast above 70 percent.
- Marine regime: onshore, offshore, neutral.
- Storm regime: none, nearby convection risk, active precip or thunder.
- Season: DJF, MAM, JJA, SON.
- Lead time: 0 to 2h, 2 to 6h, 6 to 12h, 12 to 24h.
- Boundary distance: absolute difference between provider implied max and market threshold, bucketed as 0 to 1F, 1 to 3F, greater than 3F.
- Time of day: pre-heating, heating, peak, post-peak.

Backoff order when sample is too small:

1. Drop boundary distance.
2. Drop time-of-day split.
3. Collapse wind sector from 8 bins to onshore or offshore or other.
4. Collapse cloud bucket from 3 bins to clear versus not clear.
5. Collapse storm regime to none versus active.
6. Fall back to provider-city-lead-season.
7. Fall back to provider-city global.

Minimum effective sample size for a non-fallback cell:

- Fixed MVP choice: 50 settled days.
- Provisional. Replace with a data-driven minimum chosen to keep out-of-sample bias variance below the city-specific global bias variance by at least 10 percent.

### 9.5 Uncertainty inflation

Base provider uncertainty comes from historical residual dispersion. Inflate it by:

- Provider disagreement.
- Small-sample correction from calibration bucket backoff depth.
- Observation lag penalty from current-state bridge.
- Active regime shock score.
- Recent calibration drift.

MVP formula:

- `sigma_final = sigma_base * disagreement_factor * sample_factor + lag_addon + regime_addon`
- `disagreement_factor = 1 + 0.15 * max_pairwise_mean_diff_f`
- `sample_factor = min(1.5, sqrt(max(1, 200 / n_eff)))`
- `lag_addon = 0.05F * observation_lag_minutes`
- `regime_addon = 1.0F * active_regime_haircut`

All inflation parameters are provisional and must be replaced by replay-fitted values before live eligibility.

### 9.6 Fusion logic

Provider PMFs are fused as a weighted mixture:

- `PMF_fused(T=t) = sum_i w_i * PMF_i(T=t)`

where `w_i` is the normalized product of:

- seed weight,
- reliability multiplier,
- freshness multiplier,
- source-availability multiplier.

Freshness multiplier:

- 1.0 when source is within SLA.
- 0.5 when soft stale.
- 0.0 when hard stale.

### 9.7 Threshold probability conversion

The settlement PMF is the truth surface. Threshold probability is derived from the exact market rule:

- For `YES = settlement_temp >= threshold_f`, sum PMF bins at or above threshold.
- For `YES = settlement_temp > threshold_f`, sum bins strictly above threshold.
- For `YES = settlement_temp <= threshold_f`, sum bins at or below threshold.

No midpoint approximation is allowed.

### 9.8 What is fixed versus provisional

Fixed in MVP:

- Integer Fahrenheit settlement PMF.
- Provider set of NWS, HRRR, and GEFS.
- Hierarchical conditional bias architecture.
- Fusion as weighted PMF mixture.

Provisional in MVP:

- Seed weights.
- Minimum cell sample size.
- Disagreement inflation coefficients.
- Reliability clamp range.

Data-driven replacement path:

- Replay and shadow jobs recompute weights monthly.
- Promotion rule before live: each provisional parameter must either be replaced by a fitted value supported by at least 120 settled city-days or explicitly justified as low-sensitivity with less than 5 percent EV impact under sensitivity analysis.

## 10. Nowcast Bridge Architecture

### 10.1 Purpose

The Nowcast Bridge estimates current station state between official observations. It does not forecast settlement directly. It answers: "Given the last official observation, recent station path, short-range signals, and discontinuity evidence, what is the best estimate of current station temperature and near-term slope right now, and how uncertain is that estimate?"

### 10.2 Inputs

- Last official ObservationSnapshot.
- Up to the prior three official observations within the last 90 minutes.
- HRRR 0 to 2 hour temperature, cloud, wind, and precip fields.
- Latest official weather codes and sky cover.
- Lightweight MRMS or radar reflectivity proxy near and upwind of the station.
- CityProfile fields for marine sensitivity, onshore sectors, and shock caps.
- Solar geometry for station and local time.

### 10.2.1 MVP radar or MRMS resolution

MVP uses radar or MRMS shock support with a lightweight proxy, not a full radar-ingestion stack.

Rules:

- Pull only a small upwind reflectivity summary per active city.
- Use the proxy only for shock detection and confidence downgrade, not as a primary settlement input.
- If the proxy is unavailable, continue with observation and model signals and widen shock uncertainty.
- MVP does not require full radar archive replay before shadow, but any live qualification claim about radar contribution must be supported by replay attribution.

### 10.3 Baseline interpolation and extrapolation logic

Baseline step:

1. Compute robust observation trend from the most recent observation pairs, weighted by recency.
2. Compute short-horizon model trend from HRRR between last observation time and `as_of_time`.
3. Blend the two:
   - observation trend weight 0.6
   - HRRR trend weight 0.4
4. Extrapolate last official temperature to `as_of_time`.
5. Clip physically implausible absolute rates using a provisional city-independent bound of 8F per hour.

Baseline fields:

- `temp_baseline_f`
- `trend_baseline_f_per_hr`
- `lag_minutes`

### 10.4 Non-linear cloud-cover shock adjustment

Cloud-cover shock is mandatory. Linear interpolation alone is not trusted when rapid solar suppression is plausible.

Cloud shock evidence features:

- Sky cover categorical jump into `BKN` or `OVC` since prior observation.
- HRRR cloud-cover increase over the next hour.
- Upwind reflectivity or precipitation evidence indicating an incoming cloud or storm shield.
- Solar elevation and time within effective heating window.
- City cloud sensitivity from CityProfile.

Cloud shock score:

- `cloud_score_raw = 0.45 * I(cat_jump_to_bkn_or_ovc) + 0.25 * max(0, hrrr_cloud_delta_pct) / 60 + 0.20 * I(upwind_reflectivity_gt_15dbz) + 0.10 * I(solar_elevation_gt_20deg)`
- `cloud_score = min(1, cloud_score_raw)`

Non-linear adjustment:

- `daylight_weight = 0` at night, ramps linearly to 1 once solar elevation exceeds 20 degrees.
- `cloud_cover_shock_adjustment_f = -city_profile.cloud_shock_cap_f * daylight_weight * cloud_score^1.7`

Fixed MVP caps:

- `cloud_shock_cap_f = 5F` unless overridden by city profile.

This is intentionally convex. Mild cloud evidence should not overreact. Strong clustered evidence should move the estimate materially.

### 10.5 Storm-shock handling

Storm-shock evidence features:

- Current precip or thunder weather code.
- Upwind MRMS or radar reflectivity above 25 dBZ within 20 km.
- Rapid visibility drop.
- HRRR precip onset in the next hour.

Storm score:

- `storm_score_raw = 0.35 * I(precip_code_active) + 0.25 * I(thunder_code_active) + 0.25 * max(0, reflectivity_dbz - 20) / 25 + 0.15 * I(hrrr_precip_onset_next_hour)`
- `storm_score = min(1, storm_score_raw)`

Adjustment:

- `storm_shock_adjustment_f = -city_profile.storm_shock_cap_f * daylight_weight * storm_score^1.5`

Fixed MVP cap:

- `storm_shock_cap_f = 7F`

### 10.6 Wind-shift handling

Wind-shift evidence features:

- Absolute wind direction change since prior official observation.
- Wind speed change.
- Entry into city-defined onshore sector or exit from it.

Wind-shift score:

- `wind_shift_score = min(1, 0.6 * abs(delta_dir_deg) / 90 + 0.4 * abs(delta_speed_kt) / 15)`

Adjustment:

- If shift enters the city's onshore sector, apply a negative marine-sensitive adjustment.
- If shift exits onshore flow into offshore flow and city profile marks positive warming response, allow positive adjustment.
- `wind_shift_adjustment_f = city_specific_signed_direction * city_profile.wind_shift_cap_f * wind_shift_score^1.3`

### 10.7 Marine-layer intrusion handling

Marine intrusion exists only for cities whose profile enables it.

Marine evidence features:

- Wind enters onshore sector.
- Dewpoint depression compresses by at least 4F over the last hour.
- Ceiling drops below 1500 ft or visibility drops below 5 miles.
- HRRR low-cloud increase over the coastward fetch.

Marine score:

- `marine_score = min(1, 0.30 * I(onshore_flow) + 0.25 * I(dewpoint_depression_collapse) + 0.25 * I(low_ceiling_or_vis) + 0.20 * I(hrrr_low_cloud_surge))`

Adjustment:

- `marine_intrusion_adjustment_f = -city_profile.marine_intrusion_cap_f * marine_score^1.6`

### 10.8 Observation-lag penalty and uncertainty inflation

Observation lag matters because official NWS observation feeds may be delayed. Lag penalty starts before hard stale.

Penalty:

- `observation_lag_penalty = 0` for lag up to 8 minutes.
- `observation_lag_penalty = min(1, (lag_minutes - 8) / 17)` after that.

Uncertainty:

- `sigma_now_f = 0.8F + 0.03F * lag_minutes + 1.5F * shock_risk_score + 0.5F * I(discontinuity_suspected)`

Confidence downgrade:

- `confidence_downgrade = min(0.6, 0.5 * observation_lag_penalty + 0.4 * shock_risk_score)`

### 10.9 Final output fields

CurrentStateEstimate must include:

- current_temp_est_f
- current_temp_sigma_f
- near_term_slope_f_per_hr
- observation_lag_minutes
- observation_lag_penalty
- shock_risk_score
- cloud_cover_shock_adjustment_f
- storm_shock_adjustment_f
- marine_intrusion_adjustment_f
- wind_shift_adjustment_f
- discontinuity_suspected
- confidence_downgrade

### 10.10 Separation from settlement forecasting

The bridge stops at current state. It does not output trade probability. The Path Progress Engine consumes CurrentStateEstimate and ForecastDistribution separately. This prevents nowcast error from being hidden inside outcome forecasting.

### 10.11 What proves the bridge is too weak

The nowcast bridge is considered too weak if any of the following hold on a rolling settled evaluation window of at least 50 samples:

- Overall next-observation MAE exceeds 1.5F.
- Shock-regime next-observation MAE exceeds 2.5F.
- False negative rate on discontinuities greater than 3F within one hour exceeds 20 percent.
- Mean signed error after cloud-shock detections exceeds 0.75F in absolute value.
- Confidence downgrades fail coverage: realized absolute error exceeds quoted one-sigma more than 45 percent of the time.

### 10.12 Replay and shadow recalibration

Daily after settlement:

- Compare each CurrentStateEstimate to the next official observation.
- Bucket error by city, lag bucket, cloud-shock bucket, storm-shock bucket, marine flag, and time of day.
- Update shock caps and uncertainty add-ons only when each bucket has at least 40 observations.

Redesign triggers after MVP:

- Two consecutive 30-day windows failing any threshold above.
- Any city-specific shock bucket with persistent signed bias over 1.0F.
- Any city where adding the bridge underperforms the simpler last-observation carry or linear-only baseline for 20 settled days.

## 11. Path-Dependency / Temporal Convergence Architecture

### 11.1 Dedicated path module

The daily high is monotone in one direction only. Once achieved, it cannot revert. That fact must be first-class.

The module computes:

- current temperature versus threshold
- current official high so far
- time remaining in the effective heating or cooling window
- estimated intraday slope
- threshold reachability
- late-day convergence decay
- path-aware uncertainty adjustment

### 11.2 Inputs

- SettlementRule.
- CurrentStateEstimate.
- Observation history for the current settlement window.
- ForecastDistribution.
- CityProfile typical peak hour and heating window.
- Solar insolation vector derived from solar elevation, cloud-adjusted sky state, and season.

### 11.3 Core state variables

- `current_high_so_far_f`: max official observed temperature within the settlement window.
- `threshold_gap_f`: threshold minus max(current_high_so_far_f, current_temp_est_f) for upper-threshold YES markets.
- `remaining_effective_window_minutes`: minutes until local peak window end, clipped at settlement window end.
- `estimated_intraday_slope_f_per_hr`: blend of near-term nowcast slope, HRRR next 2 hour slope, solar-insolation-driven diurnal slope, and city climatological slope for this season and time-of-day.
- `solar_insolation_vector`: compact representation of remaining cloud-adjusted daytime heating opportunity.
- `thermal_ceiling_estimate_f`: estimated practical ceiling for today's warm-side path under current cloud and wind regime.
- `residual_gain_mean_f` and `residual_gain_p80_f`: expected remaining upward move from current anchor.

### 11.4 Reachability logic

If the settlement rule is monotone upper-threshold:

- If `current_high_so_far_f >= threshold_f`, set `threshold_already_crossed_flag = true`.
- Else compute `reachability_score = sigmoid((min(residual_gain_mean_f, thermal_ceiling_estimate_f - current_temp_est_f) - threshold_gap_f) / max(1F, current_temp_sigma_f))`.

MVP blocking rule:

- If `threshold_already_crossed_flag = false` and `residual_gain_p80_f < threshold_gap_f`, block new YES trades.

### 11.5 Late-day convergence decay

Unreached warm-side thresholds become less likely as the day runs out. The module applies a decay to positive residual tail mass.

Decay factor:

- Before city peak hour: `late_day_decay_factor = 1.0`
- Between peak hour and peak hour plus 1 hour: `late_day_decay_factor = 0.85`
- More than 1 hour after peak hour and threshold still unreached: `late_day_decay_factor = 0.60`
- More than 2 hours after peak hour and threshold still unreached: `late_day_decay_factor = 0.35`

These are provisional MVP values and must be replaced by replay-fitted temperature-crossing survival curves per city and season.

### 11.6 Probability modification

Path module adjusts the PMF as follows:

1. Truncate all bins below `current_high_so_far_f`.
2. If threshold already crossed, collapse YES probability to `1 - revision_penalty`, where revision penalty defaults to 0.01 until settlement reconciliation evidence supports a lower value.
3. If threshold not crossed, multiply bins above the current anchor by `reachability_score * late_day_decay_factor`.
4. Renormalize the PMF.

Output:

- `p_model_path` from the adjusted PMF.
- `path_uncertainty_addon = min(0.25, 0.15 * (1 - reachability_score) + 0.10 * (1 - late_day_decay_factor))`

### 11.7 Haircut linkage

The EV engine consumes:

- `p_model = p_model_path`
- additional `uncertainty_haircut += path_uncertainty_addon`

This makes late-day unresolved thresholds harder to trade even if the raw pre-path forecast still looks optimistic.

## 12. Market Microstructure / Executability Architecture

### 12.1 Core requirement

The system trades executable probability, not theoretical mid.

Market data handling is streaming-first. Orderbook and trade ingestion must be driven by WebSocket streams, with REST used only for bootstrapping, resync, and metadata refresh. The decision loop is event-driven on relevant stream updates plus a bounded heartbeat; it is explicitly not a 5-minute cron architecture.

### 12.2 Orderbook representation

Kalshi binary orderbooks expose yes bids and no bids. Asks are implied:

- YES ask at price `p_yes_ask = 1 - best_no_bid`
- NO ask at price `p_no_ask = 1 - best_yes_bid`

For taker simulation:

- BUY_YES consumes the implied YES ask ladder derived from NO bids.
- BUY_NO consumes the implied NO ask ladder derived from YES bids.

### 12.3 Multi-level book depth

For a target quantity `q`, the engine computes:

- displayed WAP across price levels
- effective WAP after ghost-liquidity and durability discounts
- levels consumed
- residual unfilled quantity if effective depth is insufficient

Effective depth at each level:

- `effective_size = displayed_size * durability_multiplier * (1 - ghost_liquidity_ratio)`

### 12.4 Top-of-book durability

Durability measures whether displayed top size survives long enough to be realistic.

Metric:

- Rolling 5 minute median survival time of top-of-book size and price level.
- Additional feature: percentage of best-quote size surviving at least 500 ms and 3 seconds.

Output:

- `top_of_book_durability_score` scaled 0 to 1.

### 12.5 Ghost liquidity

Ghost liquidity is displayed size that disappears before touch often enough to make displayed depth deceptive.

MVP estimate:

- Track recent instances where displayed size at or near touch is removed within 1 second ahead of aggressive trade flow or quote pressure.
- `ghost_liquidity_ratio = removed_pre_touch_size / displayed_touch_size`, bounded [0,1].

### 12.6 Quote stability

Quote stability captures flicker risk.

Metric:

- Normalized count of best-quote price changes and top-size drops over the last 60 seconds.
- `quote_stability_score = 1 - normalized_flicker_rate`

### 12.7 Taker path

For each side and size:

- compute displayed WAP
- compute effective WAP
- derive slippage cost as `effective_wap - displayed_best_price`
- derive taker adverse-selection penalty from historical post-trade mark-out by city, time-to-close bucket, and regime

Latency realism rule:

- The system must measure decision-to-order-submit and order-submit-to-ack latency distributions in live-readonly mode before live trading.
- New taker entries are blocked if executable EV disappears under the p95 measured latency mark-out assumption.
- MVP does not assume it can win sub-10 millisecond races. If edge is only present at ultra-low-latency horizons, the correct decision is NO_TRADE.

Time-to-close buckets:

- more than 6 hours
- 3 to 6 hours
- 1 to 3 hours
- less than 1 hour

These buckets are fixed in MVP.

### 12.8 Maker path

Maker is optional after taker baseline is understood. The architecture still defines it.

Maker pre-trade outputs:

- intended resting price
- queue-ahead estimate from displayed depth
- maker fill probability before close
- maker adverse-selection penalty after fill

Maker fill probability model inputs:

- historical trade-through volume at the intended price
- current queue-ahead size
- remaining time to close
- quote stability
- regime

If live maker mode is later enabled, post-order queue position must also use Kalshi queue position endpoints to replace displayed-depth approximations.

### 12.9 Measurable outputs

MicrostructureAssessment must expose:

- best displayed price
- executable WAP by side and size
- depth_consumed_levels
- slippage_cost
- top_of_book_durability_score
- ghost_liquidity_ratio
- quote_stability_score
- maker_fill_probability
- maker_adverse_selection_penalty
- taker_adverse_selection_penalty
- tradability_score

### 12.10 Tradability score

MVP tradability score formula:

- `tradability_score = 0.30 * depth_score + 0.20 * durability_score + 0.15 * quote_stability_score + 0.15 * (1 - ghost_liquidity_ratio) + 0.20 * spread_score`

Block conditions override the score.

MVP hard blocks:

- hard stale book
- effective depth less than 3x target quantity
- spread wider than 0.08 dollars
- inside spread widens by more than 300 percent over the trailing 60 seconds
- tradability_score below 0.65

These numeric blocks are provisional. Before live they must be replaced by replay or shadow quantiles for positive-realized-EV trades.

## 13. EV / Decision Architecture

### 13.1 Variables

For each candidate action and size:

- `p_model`: path-aware YES settlement probability.
- `p_market_exec`: size-aware executable implied YES probability.
- `raw_edge`: edge before frictions.
- `fee_cost`: expected exchange fee per contract.
- `slippage_cost`: effective WAP minus ideal best executable quote.
- `adverse_selection_penalty`: expected post-fill mark-out loss.
- `total_friction = fee_cost + slippage_cost + adverse_selection_penalty`.
- `friction_to_edge_ratio = total_friction / max(raw_edge, 0.0001)`.
- `uncertainty_haircut`: haircut from calibration, lag, disagreement, and path uncertainty.
- `regime_haircut`: haircut from active regime.
- `portfolio_haircut`: correlation and exposure haircut.
- `edge_conf_adj = raw_edge * (1 - uncertainty_haircut) * (1 - regime_haircut) * (1 - portfolio_haircut)`.
- `executable_ev = edge_conf_adj - total_friction`.

Measured latency mark-out is treated as part of adverse selection penalty, not a separate optimistic afterthought.

### 13.2 Direction-specific raw edge

- For BUY_YES: `raw_edge = p_model - yes_price_exec`.
- For BUY_NO: `raw_edge = (1 - p_model) - no_price_exec`.

All prices are in dollars per one-dollar binary payoff. Therefore edge and EV are in dollars per contract and map directly to implied probability points.

### 13.3 Fee cost

Use the active fee schedule version, not a hard-coded constant.

MVP default assumption:

- General taker fee formula follows the active Kalshi schedule version.
- If the market carries maker fees later, those use the maker fee schedule version.
- Rounding and settlement-cent alignment effects are modeled separately and recorded in audit.

### 13.4 Provisional MVP thresholds

These are starting defaults only.

| Threshold | MVP default | Replacement rule before live |
| --- | --- | --- |
| Minimum raw_edge for entry | 0.03 dollars per contract | Replace with lower quartile of realized raw-edge-at-entry among positive shadow trades |
| Minimum executable_ev for entry | 0.015 dollars per contract | Replace with lower 20th percentile of realized post-friction EV among positive shadow trades |
| Maximum friction_to_edge_ratio | 0.60 | Replace with 75th percentile of realized friction_to_edge_ratio among positive shadow trades |
| Maximum uncertainty_haircut for new entry | 0.50 | Replace with coverage-calibrated threshold that preserves target reliability |
| Minimum tradability_score | 0.65 | Replace with replay-fitted threshold maximizing post-friction Sharpe subject to sample size floor |

These values are not permanent and do not qualify the system for live use.

### 13.5 Decision flow

1. Resolve market and settlement rule. If invalid, HALT_CITY.
2. Check source health and market health. If critical global issue, HALT_GLOBAL. If city-specific critical issue, HALT_CITY.
3. Compute `p_model` using forecast, nowcast, and path modules.
4. Compute tradability and microstructure metrics for BUY_YES and BUY_NO at allowed size.
5. Compute EdgeEstimate for each candidate.
6. Apply regime haircuts and blocks.
7. Apply risk and correlation vetoes.
8. Compare candidate executable EVs.
9. If no positive candidate remains:
   - return WATCH if raw edge exists but is blocked by soft conditions,
   - otherwise return NO_TRADE.
10. If an open position exists, compare HOLD versus REDUCE versus EXIT instead of automatically opening a new one.
11. Emit final StrategyDecisionExplanation.

### 13.6 Decision states

- `NO_TRADE`: default state; no candidate survives.
- `WATCH`: model edge exists but fails soft gates such as friction, uncertainty, or insufficient tradability.
- `MAKER_ONLY`: maker candidate positive and maker live eligibility passed, but taker candidate not positive.
- `TAKER_ALLOWED`: taker candidate positive and all vetoes clear.
- `REDUCE`: current position remains directionally valid but exposure or edge has decayed enough to warrant partial exit.
- `EXIT`: remaining expected EV is negative or the opposite outcome is effectively locked.
- `CANCEL_PENDING`: resting order is stale, invalidated, or blocked by new risk state.
- `HALT_CITY`: city-specific hard halt; no new trades, cancel pending, manage exits only if policy allows.
- `HALT_GLOBAL`: global hard halt; no new trades anywhere, cancel pending, freeze live adapter.

### 13.7 Existing-position logic

For an open position:

- `EXIT` if remaining executable EV for the held side is negative by more than 0.01 dollars per contract.
- `REDUCE` if remaining executable EV is positive but correlation, exposure, or regime risk exceeds allowed limits.
- `HOLD` is not emitted as a final action in MVP. A non-action on an open position is represented as `NO_TRADE` with open-position context.

### 13.8 Machine-readable explanation requirement

Every decision writes a StrategyDecisionExplanation with:

- `schema_version`
- exact numeric values for all EV components
- source freshness snapshot
- active regime and haircut
- qualification state
- block or veto reasons
- provenance references to raw and normalized inputs

## 14. Regime Architecture

MVP uses a small measurable regime set. No regime zoo.

### 14.1 CLEAR_STABLE_HEATING

| Attribute | Specification |
| --- | --- |
| Meaning | Normal daytime heating path with limited disruption risk. |
| Features | Cloud cover below 40 percent, no active precip, provider disagreement at or below 2F, observation lag at or below 8 minutes. |
| Threshold logic | All conditions above true. |
| Impact | No regime block, regime_haircut 0.00, sizing multiplier 1.00. |

### 14.2 CLOUD_SUPPRESSION_RISK

| Attribute | Specification |
| --- | --- |
| Meaning | Rapid solar suppression risk from arriving cloud bank without active convection. |
| Features | Daylight, cloud score at least 0.50, storm score below 0.40. |
| Threshold logic | `cloud_score >= 0.50` and `storm_score < 0.40`. |
| Impact | regime_haircut 0.20, maker blocked, new YES entries blocked if observation lag penalty above 0.30. |

### 14.3 CONVECTIVE_SHOCK

| Attribute | Specification |
| --- | --- |
| Meaning | Active or imminent storm disruption capable of sudden temperature path break. |
| Features | Storm score at least 0.50 or thunder or precip active with nearby radar evidence. |
| Threshold logic | `storm_score >= 0.50`. |
| Impact | regime_haircut 0.45, maker blocked, taker blocked when threshold gap is 3F or less and remaining window is under 3 hours. |

### 14.4 MARINE_INTRUSION

| Attribute | Specification |
| --- | --- |
| Meaning | Onshore cool marine air or low cloud intrusion in marine-sensitive cities. |
| Features | marine-sensitive city, marine score at least 0.45. |
| Threshold logic | `city_profile.marine_sensitive_flag = true` and `marine_score >= 0.45`. |
| Impact | regime_haircut 0.30, sizing multiplier 0.50, new upper-threshold YES entries blocked if current threshold gap is above 2F. |

### 14.5 LATE_DAY_DECAY

| Attribute | Specification |
| --- | --- |
| Meaning | Threshold still unreached after effective peak time. |
| Features | threshold not crossed, post-peak time, remaining effective window under 90 minutes. |
| Threshold logic | `threshold_already_crossed_flag = false` and `remaining_effective_window_minutes < 90` and local time beyond peak hour. |
| Impact | regime_haircut 0.35, new warm-side entries blocked, EXIT or REDUCE remains allowed. |

Every regime has direct action consequences. If a regime does not change block, haircut, or sizing, it does not exist.

## 15. Risk / Governance Architecture

Risk is a separate subsystem.

### 15.1 Fixed MVP limits

- Max positions per city: 1.
- Max open cities: 3.
- Max contracts per new live trade: 1.
- Max notional per new live trade: one contract notional only.
- Max total open live exposure: 3 contract notional equivalents.

These are fixed MVP live caps. Shadow and replay can simulate larger sizes for research.

### 15.2 Hard halts

| Halt | Trigger |
| --- | --- |
| stale-data halt | Any required source is hard stale |
| observation-lag halt | Official observation lag greater than 25 minutes |
| thin-book halt | Effective depth under 3x target quantity or spread above 0.08 dollars |
| volatility halt | inside spread widens by more than 300 percent over 60 seconds or quote flicker exceeds p99 replay baseline for that time-to-close bucket |
| provider-disagreement halt | Max pairwise provider implied daily-high disagreement above 4F and threshold gap at or below 2F |
| settlement-ambiguity halt | SettlementRule not validated, parser mismatch unresolved, or source locator unavailable |
| API degradation halt | Kalshi REST read errors above 20 percent over 60 seconds or more than 3 WebSocket rebuilds in 5 minutes |
| friction overload halt | slippage plus adverse selection exceeds raw edge or friction_to_edge_ratio exceeds limit |
| shadow underperformance halt | trailing 50 shadow trades have lower 80 percent confidence bound at or below 0 on post-friction EV |

All numeric levels above are provisional and must be replaced by empirical thresholds before live.

### 15.3 Daily loss cap

Even though MVP size is tiny, a live adapter still enforces a daily stop.

MVP default:

- Daily live loss cap = 3 settled contract notional equivalents.

Once hit:

- HALT_GLOBAL for the remainder of the local trading day.

### 15.4 Precedence rules

Highest to lowest:

1. Manual global kill switch.
2. Settlement ambiguity halt.
3. API degradation halt.
4. Global stale-source halt.
5. City halt.
6. Correlation and exposure veto.
7. Execution friction veto.
8. Soft WATCH outcome.

If any higher rule fires, lower rules are irrelevant for action selection.

## 16. Portfolio Correlation Architecture

Cities are not independent. MVP includes correlation controls even with only three cities.

### 16.1 Fixed MVP design

- Each city belongs to a static region cluster.
- Correlation matrix `R` is estimated from historical daily settlement residuals by city pair.
- Exposure is measured as signed YES-equivalent contracts.

Signed exposure:

- BUY_YES = positive exposure.
- BUY_NO = negative exposure.

Correlation-adjusted exposure:

- `portfolio_risk_units = sqrt(x^T R x)`

MVP hard block:

- Block any new live trade that would push `portfolio_risk_units` above 1.5.

### 16.2 Post-MVP upgrade path

- Replace static `R` with dynamic synoptic-pattern clustering based on contemporaneous weather regime similarity.
- Allow correlation matrix to change by season and large-scale pattern.

The dynamic exposure model is explicitly deferred until after MVP.

## 17. City Qualification Architecture

City qualification is an empirical state machine.

### 17.1 States

- `UNMAPPED`
- `OBSERVE_ONLY`
- `SHADOW_ONLY`
- `SHADOW_QUALIFIED`
- `LIVE_PILOT`
- `DISABLED`

### 17.2 Inputs

- settlement parser validation quality
- calibration quality
- nowcast bridge error
- path-model quality
- conditional bias stability
- market depth quality
- slippage profile quality
- post-friction shadow EV
- drawdown stability

### 17.3 Promotion rules

`UNMAPPED -> OBSERVE_ONLY`

- Settlement mapping validated on at least 30 settled historical markets or all available if fewer, with zero unresolved critical mismatches.

`OBSERVE_ONLY -> SHADOW_ONLY`

- Settlement parser validated on a minimum 50 settled-market corpus per city with zero critical mismatches.
- CLI `MAXIMUM` field parsing, occurrence-time parsing, and revision-monitor policy validated on that corpus.
- 14 consecutive calendar days of healthy ingestion for that city.
- Conditional bias table available at fallback level or better.
- Shock-bucket nowcast error report produced and reviewed for that city.

`SHADOW_ONLY -> SHADOW_QUALIFIED`

- At least 40 shadow decision days.
- At least 20 shadow trades.
- Threshold Brier score improvement over naive market-implied baseline.
- Rolling calibration ECE at or below 0.08.
- Nowcast next-observation MAE at or below 1.7F.
- Slippage-model error at or below 30 percent MAPE.
- Post-friction shadow EV positive on point estimate.

`SHADOW_QUALIFIED -> LIVE_PILOT`

- Must also satisfy all Section 21 live gates.

### 17.4 Demotion and disable rules

Demote to `SHADOW_ONLY` if any of the following occur over a 20-trade or 20-day rolling window:

- ECE rises above 0.10.
- nowcast MAE rises above 2.0F.
- slippage-model error rises above 40 percent.
- post-friction shadow EV turns negative.

Disable immediately if:

- settlement parser discrepancy with settled outcome is unresolved,
- required station mapping changes unexpectedly,
- repeated stale-data halts exceed 3 in 7 days,
- shadow underperformance halt is triggered.

## 18. Backtest / Replay Architecture

### 18.1 Principle

Replay is true as-of. If a value was not known by `as_of_time`, replay may not use it.

### 18.2 Required capabilities

- historical weather replay
- historical observation replay
- historical market replay
- multi-level fill simulation
- maker and taker comparison
- pessimistic, base, and optimistic friction assumptions
- incentives fixed at zero
- sensitivity analysis

### 18.3 Replay clock

The replay engine is event-driven. A replay run advances on:

- new weather observation
- new forecast issuance
- market metadata change
- orderbook delta or scheduled resync
- trade print
- scheduled evaluation checkpoints

### 18.4 Fill simulation tiers

- Tier 1 `replay_light`: uses trades, candlesticks, and sparse market snapshots only. Suitable for forecast and decision research, not for live qualification.
- Tier 2 `replay_microstructure`: uses self-captured orderbook snapshots and deltas plus trades. Required for fill realism qualification.

Live gating may only use Tier 2 evidence for microstructure claims.

### 18.5 Friction scenarios

For every replay report, compute:

- optimistic friction
- base friction
- pessimistic friction

These scenarios differ only in:

- ghost-liquidity multiplier
- slippage percentile
- adverse-selection percentile

### 18.6 Mandatory reports

- settlement reconciliation report
- settlement revision monitor report showing preliminary versus later CLI versions and any reversed highs
- forecast calibration report by city, lead bucket, and regime
- nowcast bridge error report
- nowcast bridge baseline comparison report versus last-observation carry-forward and linear-only bridge
- shock-bucket error report
- path reachability report
- taker fill realism report by time-to-close bucket
- maker viability report if maker logic is enabled in shadow
- edge waterfall report from raw edge to executable EV
- decision distribution report
- sensitivity matrix report for every provisional threshold, cap, seed weight, and uncertainty multiplier under optimistic, base, and pessimistic friction scenarios
- provisional-parameter replacement report with sample-size justification
- city qualification packet

### 18.7 Qualification rule

Replay incentives are fixed at zero. Any strategy whose attractiveness disappears once incentives are removed is disqualified.

No phase advancement is allowed after Phase 3 or Phase 4 unless every provisional parameter satisfies one of the following:

- replaced by a data-driven value with explicit sample-size justification, or
- shown by the sensitivity matrix to have less than 5 percent impact on executable EV under pessimistic scenarios.

## 19. Shadow Trading Architecture

Shadow mode is both a validation layer and a feedback loop.

### 19.1 Shadow execution behavior

- Use the same decision stack as live.
- Never place real orders.
- Produce ShadowFill and ShadowPosition objects.
- Reconcile modeled fills and PnL after actual market evolution and settlement.
- Do not begin shadow trading for a city until the Phase 2 baseline comparison report and shock-bucket error report exist for that city.

### 19.2 Shadow fill logic

Taker shadow fill:

- Execute immediately at effective WAP from the microstructure engine.
- Record optimistic, base, and pessimistic fill scenarios.

Maker shadow fill, when enabled later:

- Record intended price and queue-ahead estimate.
- Mark filled only if observed trade-through and queue-depletion evidence supports it under modeled assumptions.

### 19.3 Shadow feedback updates

Daily after settlement, shadow updates:

- uncertainty haircuts
- city qualification
- regime qualification
- nowcast bridge tuning
- friction threshold tuning
- maker viability
- taker viability
- calibration drift

Update rules:

- Haircuts update only from settled windows.
- Qualification changes are sticky until the next daily governance batch.
- No single day may permanently tighten or loosen live thresholds without the minimum sample specified in Section 21.

## 20. Drift / Monitoring Architecture

The system monitors:

- calibration drift
- provider lag
- nowcast bridge error
- market ingestion instability
- fill-quality drift
- decision-distribution drift
- settlement-parser discrepancies
- explanation schema drift

### 20.1 Metrics

Calibration drift:

- rolling ECE, Brier score, and log loss by city and lead bucket

Provider lag:

- percent of observations and forecasts within freshness SLA

Nowcast bridge error:

- next-observation MAE and signed error by shock bucket

Market ingestion instability:

- WebSocket reconnect count
- sequence-gap count
- stale-book duration
- flash-spread events

Source timing drift:

- rolling distribution of `ingest_time - event_time` by source
- automatic uncertainty widening when drift breaches source-specific control bands

Fill-quality drift:

- realized versus modeled slippage
- realized versus modeled adverse selection

Decision-distribution drift:

- share of HALT, NO_TRADE, WATCH, and TAKER_ALLOWED decisions versus trailing 30-day baseline

Settlement-parser discrepancies:

- any mismatch between parsed final official source and exchange-determined outcome
- any settlement reversal caused by later CLI revisions after a preliminary report

Explanation schema drift:

- schema_version
- JSON schema hash
- non-null field coverage

### 20.2 Alert severities

- info: metric drift but below qualification threshold
- warn: soft breach; review required
- critical: hard halt or disable condition

## 21. Live Gating Architecture

Live trading is blocked until all hard gates are met.

### 21.1 Hard live gates

- Minimum total shadow sample: 100 shadow trades.
- Minimum city-specific shadow sample: 30 shadow trades per city.
- Minimum regime-specific sample: 15 shadow trades in any regime allowed for live trading; regimes below sample minimum remain blocked live.
- Positive post-friction shadow EV: lower 80 percent confidence bound above 0.
- Positive post-adverse-selection shadow EV: lower 80 percent confidence bound above 0.
- Stable calibration error: rolling ECE at or below 0.06 over the last 100 shadow decisions.
- Stable nowcast bridge error: overall next-observation MAE at or below 1.5F and shock-bucket MAE at or below 2.5F.
- Stable ingestion freshness: at least 99 percent of required source records within soft SLA over 14 days.
- No unresolved reconciliation issues: zero unresolved settlement-parser mismatches.
- No incentive dependence: all qualification metrics computed with incentives and rebates set to zero.
- All provisional thresholds, caps, weights, and uncertainty multipliers replaced or explicitly justified by replay or shadow evidence.
- Sensitivity matrix shows any remaining provisional parameter has less than 5 percent executable-EV impact under pessimistic scenarios.

### 21.2 Additional live-start constraints

- RunMode must pass from `LIVE_READONLY` to `LIVE_TRADE` manually.
- Initial live size remains one contract.
- Maker path remains disabled until separate maker gates pass.

## 22. MVP Scope

MVP is intentionally narrow:

- maximum 3 cities
- single-threshold binary markets only
- one position per city
- one contract max per live trade
- taker-first execution realism
- maker shadow only, unless separately unlocked later
- no spread or combo execution
- no consumer weather apps in core logic
- no automatic city expansion
- no heavy ML
- no hidden incentive assumptions

## 23. What Must Be Deferred Until After MVP

- multi-leg spread execution
- dynamic hedge overlays
- broad city expansion
- advanced ML before baseline calibration is proven
- aggressive maker strategies before maker realism is understood
- dynamic synoptic-pattern exposure model
- satellite-heavy shock models that exceed MVP ops complexity if baseline nowcast is already adequate
- RFQ-driven execution logic
- portfolio optimization beyond one-position-per-city

## 24. Failure Modes and Defenses

| Failure mode | Defense |
| --- | --- |
| wrong settlement mapping | Settlement Rule Resolver plus historical settlement validation harness; hard halt on ambiguity |
| final revised CLI changes the apparent outcome overnight | Settlement Revision Monitor stores every CLI version, parses `MAXIMUM` explicitly, and leaves reconciliation unresolved until the final applicable report version is observed |
| stale observations | lag penalty, hard stale halt, METAR cross-check, no-trade default |
| observation lag | explicit observation_lag_penalty and hard halt above 25 minutes |
| cloud-cover shock or storm shock not yet reflected | shock-aware nowcast with non-linear cloud adjustment, storm score, and confidence downgrade |
| nowcast bridge miss | bridge error monitoring, redesign triggers, and replay comparison against simpler baselines |
| provider disagreement | provider-disagreement halt and uncertainty inflation |
| ghost liquidity | effective depth discount, tradability score, pessimistic fill scenarios |
| maker adverse selection | maker blocked in MVP live, explicit adverse-selection penalty in shadow |
| taker slippage blowout | multi-level WAP, depth thresholds, friction overload halt |
| flash spread event | volatility halt on rapid spread expansion plus quote-stability deterioration |
| rate-limit issues | adapter throttling, backoff, cached state, API degradation halt |
| portfolio correlation blowup | static correlation matrix and correlation-adjusted exposure cap |
| calibration drift | rolling ECE and qualification demotion |
| hidden dependence on incentives | qualification metrics always computed with incentives fixed at zero |
| explanation-schema drift | schema_version, schema hash monitoring, replay compatibility checks |

## 25. Phased Build Plan

### Phase 1: Settlement truth and ingestion skeleton

| Item | Specification |
| --- | --- |
| Objective | Build the source-of-truth and raw capture foundation. |
| Deliverables | Reference registry, settlement rule parser, settlement revision monitor, raw weather adapters, raw Kalshi market and orderbook capture, normalization skeleton, frozen StrategyDecisionExplanation schema v1.0.0, source-health monitoring. |
| Dependencies | Confirmed city list and Kalshi credentials for read access. |
| Acceptance criteria | At least one city can be ingested end-to-end with raw and normalized stores populated; settlement rules parse into a validated schema; the settlement validation harness reproduces historical settled outcomes with zero unresolved mismatches; CLI `MAXIMUM` and occurrence-time parsing work across multiple report versions; StrategyDecisionExplanation schema v1.0.0 is frozen; orderbook capture survives reconnects. |
| Risk if skipped | Everything downstream can look precise while being built on the wrong settlement variable or incomplete data. |

### Phase 2: Forecast baseline and shock-aware nowcast bridge

| Item | Specification |
| --- | --- |
| Objective | Produce the first calibrated settlement PMF and a non-linear current-state bridge. |
| Deliverables | NWS, HRRR, and GEFS normalization; seed-weight fusion; conditional bias tables; nowcast bridge with cloud-cover shock, storm shock, wind shift, marine intrusion, lag penalty, and confidence downgrade. |
| Dependencies | Phase 1 raw and normalized weather data. |
| Acceptance criteria | Forecast PMF generated per city-day; nowcast bridge generates all required output fields; replay against next-observation targets shows improvement over both last-observation carry-forward and linear-only baselines; shock-bucket error report exists for every MVP city before any shadow trades. |
| Risk if skipped | The system will systematically miss the exact intraday discontinuities that most often create false edge. |

Evidence forcing upgrade or redesign after Phase 2:

- nowcast bridge fails the thresholds in Section 10.11,
- non-linear cloud adjustment does not beat linear-only in out-of-sample replay,
- shock false negatives remain too high for live trust.

### Phase 3: Path conditioning and taker microstructure realism

| Item | Specification |
| --- | --- |
| Objective | Convert baseline forecasts into executable decisions. |
| Deliverables | Path Progress Engine, microstructure engine, tradability score, EV engine, risk and governance engine, explanation schema v1.0.0. |
| Dependencies | Phase 2 outputs plus continuous orderbook and trade capture. |
| Acceptance criteria | For replay and live-readonly, the system emits StrategyDecisionExplanation objects with candidate actions and explicit NO_TRADE defaults; replay emits the full sensitivity matrix for every provisional parameter. |
| Risk if skipped | The platform devolves into forecast watching without execution realism. |

### Phase 4: Replay reports and shadow trading

| Item | Specification |
| --- | --- |
| Objective | Validate the stack and begin empirical feedback. |
| Deliverables | True as-of replay runner, shadow execution engine, qualification reports, drift dashboards, city qualification state machine. |
| Dependencies | Phase 3 full decision stack. |
| Acceptance criteria | Replay and shadow produce the mandatory reports in Sections 18 and 19; city states can promote and demote automatically from settled evidence; no provisional parameter remains unexplained without replacement evidence or a less-than-5-percent pessimistic-scenario EV impact result. |
| Risk if skipped | There is no honest way to distinguish lucky narratives from robust edge. |

### Phase 5: Thin live adapter

| Item | Specification |
| --- | --- |
| Objective | Add live execution only after evidence passes. |
| Deliverables | Live adapter, order idempotency, cancel logic, readback reconciliation, live-readonly mode, manual enable path to live-trade mode. |
| Dependencies | All live gates in Section 21 must pass. |
| Acceptance criteria | Live adapter can create, cancel, and reconcile one-contract trades without changing model logic and with full audit coverage. |
| Risk if skipped | The system cannot graduate from research, but that is safer than live-trading prematurely. |

## 26. Exact Open Questions That Must Be Resolved Before Coding

1. Which exact three cities are in MVP, and are any of them marine-sensitive enough to require city-specific marine-layer logic from day one?
2. For each target series, what is the exact Kalshi market ticker pattern and do the market rules or outcome verification links ever change station or wording across days?
3. What is the final authoritative station mapping for each city, including the exact climate product identifier and NWS station API identifier?
4. Is the intended research horizon same-day only, or do we need overnight and pre-open support robust enough to make GEFS weight more important than the current MVP seed table assumes?
5. Do we have permission and credentials to run against production read endpoints, demo trading endpoints, or both?
6. Do we have enough historical Kalshi weather market coverage per city to validate settlement parsing on at least 50 settled markets, or which MVP cities need a temporary research-only exception before shadow eligibility?
7. Do we have or can we obtain historical full-depth orderbook archives, or must qualifying microstructure replay begin only after we start self-capturing live orderbook data?
8. What deployment topology is intended for MVP: single local workstation, one always-on VPS, or a managed cloud host with persistent storage and clock synchronization controls?
9. Do you want me to lock the first implementation to StrategyDecisionExplanation schema `1.0.0` exactly as defined here, or do you want one final schema review pass before coding begins?
