from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from kalshi_weather.domain.enums import QualificationState
from kalshi_weather.domain.models import (
    CityProfile,
    EdgeEstimate,
    ForecastSnapshot,
    MarketSnapshot,
    ObservationSnapshot,
    OrderbookSnapshot,
    PathProgressState,
    RegimeAssessment,
    ShadowPosition,
    StationReference,
    TradeSnapshot,
)
from kalshi_weather.engines.decision import _threshold_concentration_summary, run_market_decision_cycle
from kalshi_weather.market.payloads import market_definition_from_payload
from kalshi_weather.portfolio import build_static_correlation_matrix


class DecisionCycleTest(unittest.TestCase):
    def test_run_market_decision_cycle_returns_explanation(self) -> None:
        now = datetime.now(timezone.utc)
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "event_ticker": "KXHIGHNY-26APR04",
            "series_ticker": "KXHIGHNY",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
            "price_level_structure": "linear_cent",
            "open_time": now.isoformat(),
            "close_time": now.isoformat(),
            "status": "open",
        }
        market_definition = market_definition_from_payload(market_payload)
        market_snapshot = MarketSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            status="open",
            open_time=now,
            close_time=now,
            settlement_ts=None,
            yes_bid_dollars=Decimal("0.10"),
            yes_ask_dollars=Decimal("0.12"),
            no_bid_dollars=Decimal("0.88"),
            no_ask_dollars=Decimal("0.90"),
            yes_bid_size_fp=Decimal("10"),
            yes_ask_size_fp=Decimal("10"),
            no_bid_size_fp=Decimal("10"),
            no_ask_size_fp=Decimal("10"),
            last_price_dollars=Decimal("0.11"),
            last_trade_size_fp=Decimal("1"),
            volume_fp=Decimal("100"),
            open_interest_fp=Decimal("50"),
            updated_time=now,
            source_payload_id="m1",
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now,
                ingest_time=now,
                temperature_f=Decimal("70"),
                dewpoint_f=Decimal("50"),
                wind_dir_deg=290,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="SCT",
                ceiling_ft=5000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs1",
            ),
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now.replace(hour=max(0, now.hour - 1)),
                ingest_time=now,
                temperature_f=Decimal("68"),
                dewpoint_f=Decimal("49"),
                wind_dir_deg=280,
                wind_speed_kt=Decimal("6"),
                sky_cover_code="FEW",
                ceiling_ft=6000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs2",
            ),
        ]
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=now,
                ingest_time=now,
                valid_for_times=(now, now.replace(hour=min(23, now.hour + 1))),
                hourly_temp_path_f=(Decimal("72"), Decimal("75")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("30")),
                wind_path=(Decimal("8"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="f1",
            )
        ]
        orderbook = OrderbookSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            as_of_time=now,
            seq=0,
            yes_bids_ladder=((Decimal("0.10"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.90"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.10"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.90"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )
        trades = [
            TradeSnapshot(
                trade_id="t1",
                market_ticker="KXHIGHNY-26APR04-T75",
                created_time=now,
                count_fp=Decimal("1"),
                yes_price_dollars=Decimal("0.11"),
                no_price_dollars=Decimal("0.89"),
                taker_side="yes",
                source_payload_id="tr1",
            )
        ]
        result = run_market_decision_cycle(
            market=market_snapshot,
            market_definition=market_definition,
            orderbook=orderbook,
            recent_orderbooks=[orderbook],
            recent_trades=trades,
            observations=observations,
            forecasts=forecasts,
            city_profile=city,
            station=station,
            qualification_state=QualificationState.SHADOW_ONLY,
        )
        self.assertEqual(result.explanation.schema_version, "1.0.0")
        self.assertEqual(result.explanation.city_id, "nyc")

    def test_regime_block_flag_vetoes_new_trade(self) -> None:
        now = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "event_ticker": "KXHIGHNY-26APR04",
            "series_ticker": "KXHIGHNY",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
            "price_level_structure": "linear_cent",
            "open_time": now.isoformat(),
            "close_time": now.isoformat(),
            "status": "open",
        }
        market_definition = market_definition_from_payload(market_payload)
        market_snapshot = MarketSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            status="open",
            open_time=now,
            close_time=now,
            settlement_ts=None,
            yes_bid_dollars=Decimal("0.60"),
            yes_ask_dollars=Decimal("0.62"),
            no_bid_dollars=Decimal("0.38"),
            no_ask_dollars=Decimal("0.40"),
            yes_bid_size_fp=Decimal("10"),
            yes_ask_size_fp=Decimal("10"),
            no_bid_size_fp=Decimal("10"),
            no_ask_size_fp=Decimal("10"),
            last_price_dollars=Decimal("0.61"),
            last_trade_size_fp=Decimal("1"),
            volume_fp=Decimal("100"),
            open_interest_fp=Decimal("50"),
            updated_time=now,
            source_payload_id="m1",
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now,
                ingest_time=now,
                temperature_f=Decimal("74"),
                dewpoint_f=Decimal("50"),
                wind_dir_deg=290,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="SCT",
                ceiling_ft=5000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs1",
            ),
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now.replace(hour=11),
                ingest_time=now,
                temperature_f=Decimal("73"),
                dewpoint_f=Decimal("49"),
                wind_dir_deg=280,
                wind_speed_kt=Decimal("6"),
                sky_cover_code="FEW",
                ceiling_ft=6000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs2",
            ),
        ]
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=now,
                ingest_time=now,
                valid_for_times=(now, now.replace(hour=13), now.replace(hour=14)),
                hourly_temp_path_f=(Decimal("75"), Decimal("77"), Decimal("78")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("20"), Decimal("20")),
                wind_path=(Decimal("8"), Decimal("9"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="f1",
            )
        ]
        orderbook = OrderbookSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            as_of_time=now,
            seq=0,
            yes_bids_ladder=((Decimal("0.60"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.40"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.62"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.42"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )
        trades = [
            TradeSnapshot(
                trade_id="t1",
                market_ticker="KXHIGHNY-26APR04-T75",
                created_time=now,
                count_fp=Decimal("1"),
                yes_price_dollars=Decimal("0.61"),
                no_price_dollars=Decimal("0.39"),
                taker_side="yes",
                source_payload_id="tr1",
            )
        ]
        with patch(
            "kalshi_weather.engines.decision.build_regime_assessment",
            return_value=RegimeAssessment(
                city_id="nyc",
                as_of_time=now,
                active_regime="LATE_DAY_DECAY",
                regime_scores={"LATE_DAY_DECAY": Decimal("1")},
                feature_values={},
                block_flag=True,
                haircut_value=Decimal("0.35"),
                sizing_multiplier=Decimal("1"),
                explanation_codes=("late_day_decay",),
            ),
        ):
            result = run_market_decision_cycle(
                market=market_snapshot,
                market_definition=market_definition,
                orderbook=orderbook,
                recent_orderbooks=[orderbook],
                recent_trades=trades,
                observations=observations,
                forecasts=forecasts,
                city_profile=city,
                station=station,
                qualification_state=QualificationState.SHADOW_ONLY,
                as_of_time=now,
            )
        self.assertEqual(result.explanation.final_decision.value, "NO_TRADE")
        self.assertIn("regime_hard_block_late_day_decay", result.explanation.explanation_codes)

    def test_run_market_decision_cycle_respects_as_of_time_and_filters_future_forecasts(self) -> None:
        now = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "event_ticker": "KXHIGHNY-26APR04",
            "series_ticker": "KXHIGHNY",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
            "price_level_structure": "linear_cent",
            "open_time": now.isoformat(),
            "close_time": now.isoformat(),
            "status": "open",
        }
        market_definition = market_definition_from_payload(market_payload)
        market_snapshot = MarketSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            status="open",
            open_time=now,
            close_time=now,
            settlement_ts=None,
            yes_bid_dollars=Decimal("0.10"),
            yes_ask_dollars=Decimal("0.12"),
            no_bid_dollars=Decimal("0.88"),
            no_ask_dollars=Decimal("0.90"),
            yes_bid_size_fp=Decimal("10"),
            yes_ask_size_fp=Decimal("10"),
            no_bid_size_fp=Decimal("10"),
            no_ask_size_fp=Decimal("10"),
            last_price_dollars=Decimal("0.11"),
            last_trade_size_fp=Decimal("1"),
            volume_fp=Decimal("100"),
            open_interest_fp=Decimal("50"),
            updated_time=now,
            source_payload_id="m1",
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now,
                ingest_time=now,
                temperature_f=Decimal("70"),
                dewpoint_f=Decimal("50"),
                wind_dir_deg=290,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="SCT",
                ceiling_ft=5000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs1",
            ),
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now.replace(hour=11),
                ingest_time=now,
                temperature_f=Decimal("68"),
                dewpoint_f=Decimal("49"),
                wind_dir_deg=280,
                wind_speed_kt=Decimal("6"),
                sky_cover_code="FEW",
                ceiling_ft=6000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs2",
            ),
        ]
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=now.replace(hour=11),
                ingest_time=now.replace(hour=11),
                valid_for_times=(now, now.replace(hour=13)),
                hourly_temp_path_f=(Decimal("72"), Decimal("75")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("30")),
                wind_path=(Decimal("8"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="f1",
            ),
            ForecastSnapshot(
                provider_id="NWS_GRID",
                provider_run_time=now.replace(hour=13),
                ingest_time=now.replace(hour=13),
                valid_for_times=(now.replace(hour=13), now.replace(hour=14)),
                hourly_temp_path_f=(Decimal("82"), Decimal("85")),
                cloud_cover_path_pct=(Decimal("90"), Decimal("90")),
                wind_path=(Decimal("8"), Decimal("9")),
                precipitation_path=(Decimal("80"), Decimal("80")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="f2",
            ),
        ]
        orderbook = OrderbookSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            as_of_time=now,
            seq=0,
            yes_bids_ladder=((Decimal("0.10"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.90"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.10"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.90"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )
        result = run_market_decision_cycle(
            market=market_snapshot,
            market_definition=market_definition,
            orderbook=orderbook,
            recent_orderbooks=[orderbook],
            recent_trades=[],
            observations=observations,
            forecasts=forecasts,
            city_profile=city,
            station=station,
            qualification_state=QualificationState.SHADOW_ONLY,
            as_of_time=now,
        )
        self.assertEqual(result.explanation.as_of_time, now)
        self.assertEqual(result.explanation.forecast_summary["forecast_provider_count"], 1)

    def test_open_position_negative_ev_triggers_exit(self) -> None:
        now = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "event_ticker": "KXHIGHNY-26APR04",
            "series_ticker": "KXHIGHNY",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
            "price_level_structure": "linear_cent",
            "open_time": now.isoformat(),
            "close_time": now.isoformat(),
            "status": "open",
        }
        market_definition = market_definition_from_payload(market_payload)
        market_snapshot = MarketSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            status="open",
            open_time=now,
            close_time=now,
            settlement_ts=None,
            yes_bid_dollars=Decimal("0.08"),
            yes_ask_dollars=Decimal("0.90"),
            no_bid_dollars=Decimal("0.08"),
            no_ask_dollars=Decimal("0.90"),
            yes_bid_size_fp=Decimal("10"),
            yes_ask_size_fp=Decimal("10"),
            no_bid_size_fp=Decimal("10"),
            no_ask_size_fp=Decimal("10"),
            last_price_dollars=Decimal("0.90"),
            last_trade_size_fp=Decimal("1"),
            volume_fp=Decimal("100"),
            open_interest_fp=Decimal("50"),
            updated_time=now,
            source_payload_id="m1",
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now,
                ingest_time=now,
                temperature_f=Decimal("60"),
                dewpoint_f=Decimal("48"),
                wind_dir_deg=290,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="OVC",
                ceiling_ft=4000,
                visibility_mi=Decimal("10"),
                weather_codes=("CLOUDY",),
                quality_flags=(),
                source_payload_id="obs1",
            ),
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now.replace(hour=11),
                ingest_time=now,
                temperature_f=Decimal("59"),
                dewpoint_f=Decimal("47"),
                wind_dir_deg=280,
                wind_speed_kt=Decimal("7"),
                sky_cover_code="BKN",
                ceiling_ft=4500,
                visibility_mi=Decimal("10"),
                weather_codes=("CLOUDY",),
                quality_flags=(),
                source_payload_id="obs2",
            ),
        ]
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=now,
                ingest_time=now,
                valid_for_times=(now, now.replace(hour=13)),
                hourly_temp_path_f=(Decimal("61"), Decimal("62")),
                cloud_cover_path_pct=(Decimal("95"), Decimal("98")),
                wind_path=(Decimal("8"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="f1",
            )
        ]
        orderbook = OrderbookSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            as_of_time=now,
            seq=0,
            yes_bids_ladder=((Decimal("0.08"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.08"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.90"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.90"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )
        result = run_market_decision_cycle(
            market=market_snapshot,
            market_definition=market_definition,
            orderbook=orderbook,
            recent_orderbooks=[orderbook],
            recent_trades=[],
            observations=observations,
            forecasts=forecasts,
            city_profile=city,
            station=station,
            qualification_state=QualificationState.SHADOW_ONLY,
            open_positions=[
                (
                    "nyc",
                    ShadowPosition(
                        city_id="nyc",
                        market_ticker="KXHIGHNY-26APR04-T75",
                        side="yes",
                        open_quantity_fp=Decimal("1"),
                        avg_cost_dollars=Decimal("0.45"),
                        cumulative_fees_dollars=Decimal("0.01"),
                        mark_pnl_dollars=Decimal("0"),
                        settled_pnl_dollars=Decimal("0"),
                        lifecycle_status="OPEN",
                    ),
                )
            ],
            as_of_time=now,
        )
        self.assertEqual(result.explanation.final_decision.value, "EXIT")
        self.assertEqual(result.explanation.risk_summary["open_position_side"], "yes")

    def test_regime_block_flag_forces_exit_on_same_market_open_position(self) -> None:
        now = datetime(2026, 4, 6, 12, tzinfo=timezone.utc)
        station = StationReference(
            station_id="nyc-central-park",
            station_name="Central Park",
            metar_code="KNYC",
            nws_station_api_id="KNYC",
            climate_product_id="CLINYC",
            latitude=Decimal("40.7829"),
            longitude=Decimal("-73.9654"),
            timezone="America/New_York",
            wfo_office="OKX",
            grid_x=33,
            grid_y=37,
            climate_timezone_basis="LOCAL_STANDARD_TIME",
        )
        city = CityProfile(
            city_id="nyc",
            display_name="New York City",
            station_id="nyc-central-park",
            region_cluster="northeast",
            marine_sensitive_flag=True,
            onshore_wind_sectors=(90, 100, 110),
            typical_peak_hour_local_by_season={"MAM": 15},
            heating_window_by_season={"MAM": (9, 17)},
            cloud_shock_cap_f=Decimal("5"),
            storm_shock_cap_f=Decimal("7"),
            marine_intrusion_cap_f=Decimal("6"),
            wind_shift_cap_f=Decimal("4"),
            calibration_buckets_version="seed_v1",
        )
        market_payload = {
            "ticker": "KXHIGHNY-26APR04-T75",
            "event_ticker": "KXHIGHNY-26APR04",
            "series_ticker": "KXHIGHNY",
            "rules_primary": "If the highest temperature recorded in Central Park, New York for April 04, 2026 as reported by the National Weather Service's Climatological Report (Daily), is greater than 75°, then the market resolves to Yes.",
            "price_level_structure": "linear_cent",
            "open_time": now.isoformat(),
            "close_time": now.isoformat(),
            "status": "open",
        }
        market_definition = market_definition_from_payload(market_payload)
        market_snapshot = MarketSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            status="open",
            open_time=now,
            close_time=now,
            settlement_ts=None,
            yes_bid_dollars=Decimal("0.60"),
            yes_ask_dollars=Decimal("0.62"),
            no_bid_dollars=Decimal("0.38"),
            no_ask_dollars=Decimal("0.40"),
            yes_bid_size_fp=Decimal("10"),
            yes_ask_size_fp=Decimal("10"),
            no_bid_size_fp=Decimal("10"),
            no_ask_size_fp=Decimal("10"),
            last_price_dollars=Decimal("0.61"),
            last_trade_size_fp=Decimal("1"),
            volume_fp=Decimal("100"),
            open_interest_fp=Decimal("50"),
            updated_time=now,
            source_payload_id="m1",
        )
        observations = [
            ObservationSnapshot(
                station_id="nyc-central-park",
                event_time=now,
                ingest_time=now,
                temperature_f=Decimal("74"),
                dewpoint_f=Decimal("50"),
                wind_dir_deg=290,
                wind_speed_kt=Decimal("8"),
                sky_cover_code="SCT",
                ceiling_ft=5000,
                visibility_mi=Decimal("10"),
                weather_codes=("FAIR",),
                quality_flags=(),
                source_payload_id="obs1",
            ),
        ]
        forecasts = [
            ForecastSnapshot(
                provider_id="NWS",
                provider_run_time=now,
                ingest_time=now,
                valid_for_times=(now, now.replace(hour=13), now.replace(hour=14)),
                hourly_temp_path_f=(Decimal("75"), Decimal("77"), Decimal("78")),
                cloud_cover_path_pct=(Decimal("20"), Decimal("20"), Decimal("20")),
                wind_path=(Decimal("8"), Decimal("9"), Decimal("9")),
                precipitation_path=(Decimal("0"), Decimal("0"), Decimal("0")),
                provider_metadata={"station_id": "nyc-central-park"},
                source_payload_id="f1",
            )
        ]
        orderbook = OrderbookSnapshot(
            market_ticker="KXHIGHNY-26APR04-T75",
            as_of_time=now,
            seq=0,
            yes_bids_ladder=((Decimal("0.60"), Decimal("10")),),
            no_bids_ladder=((Decimal("0.40"), Decimal("10")),),
            implied_yes_asks_ladder=((Decimal("0.62"), Decimal("10")),),
            implied_no_asks_ladder=((Decimal("0.42"), Decimal("10")),),
            checksum_status="rest_snapshot",
            source_refs=("ob1",),
        )

        with patch(
            "kalshi_weather.engines.decision.build_regime_assessment",
            return_value=RegimeAssessment(
                city_id="nyc",
                as_of_time=now,
                active_regime="CONVECTIVE_SHOCK",
                regime_scores={"CONVECTIVE_SHOCK": Decimal("1")},
                feature_values={},
                block_flag=True,
                haircut_value=Decimal("0.45"),
                sizing_multiplier=Decimal("1"),
                explanation_codes=("convective_shock",),
            ),
        ):
            result = run_market_decision_cycle(
                market=market_snapshot,
                market_definition=market_definition,
                orderbook=orderbook,
                recent_orderbooks=[orderbook],
                recent_trades=[],
                observations=observations,
                forecasts=forecasts,
                city_profile=city,
                station=station,
                qualification_state=QualificationState.SHADOW_ONLY,
                open_positions=[
                    (
                        "nyc",
                        ShadowPosition(
                            city_id="nyc",
                            market_ticker="KXHIGHNY-26APR04-T75",
                            side="yes",
                            open_quantity_fp=Decimal("1"),
                            avg_cost_dollars=Decimal("0.45"),
                            cumulative_fees_dollars=Decimal("0.01"),
                            mark_pnl_dollars=Decimal("0"),
                            settled_pnl_dollars=Decimal("0"),
                            lifecycle_status="OPEN",
                        ),
                    )
                ],
                as_of_time=now,
            )

        self.assertEqual(result.explanation.final_decision.value, "EXIT")
        self.assertIn("exit_due_regime_hard_block_convective_shock", result.explanation.explanation_codes)

    def test_threshold_concentration_summary_flags_similar_cross_city_exposure(self) -> None:
        selected_edge = EdgeEstimate(
            market_ticker="KXHIGHNY-26APR06-T54",
            side="no",
            quantity_fp=Decimal("1"),
            p_model=Decimal("0.78"),
            p_market_exec=Decimal("0.70"),
            raw_edge=Decimal("0.08"),
            fee_cost=Decimal("0.01"),
            slippage_cost=Decimal("0.00"),
            adverse_selection_penalty=Decimal("0.01"),
            total_friction=Decimal("0.02"),
            friction_to_edge_ratio=Decimal("0.25"),
            uncertainty_haircut=Decimal("0.20"),
            regime_haircut=Decimal("0"),
            portfolio_haircut=Decimal("0"),
            edge_conf_adj=Decimal("0.06"),
            executable_ev_per_contract=Decimal("0.04"),
            executable_ev_total=Decimal("0.04"),
        )
        path_state = PathProgressState(
            as_of_time=datetime(2026, 4, 6, 16, tzinfo=timezone.utc),
            current_high_so_far_f=Decimal("52"),
            current_temp_f=Decimal("52"),
            threshold_gap_f=Decimal("2"),
            remaining_effective_window_minutes=120,
            estimated_intraday_slope_f_per_hr=Decimal("0.5"),
            solar_insolation_vector={"daylight_weight": Decimal("1"), "minutes_to_peak": Decimal("30")},
            thermal_ceiling_estimate_f=Decimal("55"),
            residual_gain_mean_f=Decimal("1.8"),
            residual_gain_p80_f=Decimal("2.5"),
            reachability_score=Decimal("0.40"),
            late_day_decay_factor=Decimal("0.85"),
            path_uncertainty_addon=Decimal("0.15"),
            threshold_already_crossed_flag=False,
        )
        regime = RegimeAssessment(
            city_id="nyc",
            as_of_time=datetime(2026, 4, 6, 16, tzinfo=timezone.utc),
            active_regime="LATE_DAY_DECAY",
            regime_scores={"LATE_DAY_DECAY": Decimal("1")},
            feature_values={},
            block_flag=False,
            haircut_value=Decimal("0.10"),
            sizing_multiplier=Decimal("1"),
            explanation_codes=("late_day_decay",),
        )
        open_positions = [
            (
                "chi",
                ShadowPosition(
                    city_id="chi",
                    market_ticker="KXHIGHCHI-26APR06-T47",
                    side="no",
                    open_quantity_fp=Decimal("1"),
                    avg_cost_dollars=Decimal("0.80"),
                    cumulative_fees_dollars=Decimal("0.01"),
                    mark_pnl_dollars=Decimal("0"),
                    settled_pnl_dollars=Decimal("0"),
                    lifecycle_status="OPEN",
                ),
            )
        ]
        open_position_signals = [
            {
                "city_id": "chi",
                "market_ticker": "KXHIGHCHI-26APR06-T47",
                "as_of_time": "2026-04-06T15:55:00+00:00",
                "edge_summary": {
                    "selected_side": "no",
                    "selected_p_model": "0.79",
                },
                "path_state": {
                    "threshold_gap_f": "2.2",
                },
                "regime_summary": {
                    "active_regime": "LATE_DAY_DECAY",
                },
            }
        ]
        summary = _threshold_concentration_summary(
            city_id="nyc",
            market_ticker="KXHIGHNY-26APR06-T54",
            selected_edge=selected_edge,
            path_state=path_state,
            regime=regime,
            open_positions=open_positions,
            open_position_signals=open_position_signals,
            correlation_matrix=build_static_correlation_matrix(["nyc", "chi"]),
        )
        self.assertTrue(summary["threshold_concentration_block"])
        self.assertEqual(summary["threshold_concentration_city"], "chi")


if __name__ == "__main__":
    unittest.main()
