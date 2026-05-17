from __future__ import annotations

from datetime import datetime, timezone
import json
import tempfile
import unittest

from kalshi_weather.ingestion.contracts import RawPayloadRecord
from kalshi_weather.storage.derived_store import DerivedAnalyticsStore
from kalshi_weather.storage.raw_store import FileRawStore
from kalshi_weather.storage.reference_registry import FileReferenceRegistry


class StorageTest(unittest.TestCase):
    def test_raw_store_writes_payload_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FileRawStore(tmpdir)
            stored = store.write(
                RawPayloadRecord(
                    source_name="unit_test",
                    source_endpoint="https://example.test",
                    request_params={},
                    transport_status=200,
                    payload_hash="ignored",
                    parser_version="v1",
                    ingest_time=datetime.now(timezone.utc),
                    event_time=None,
                    payload="hello",
                )
            )
            self.assertTrue(stored.metadata_path.exists())
            self.assertTrue(stored.payload_path.exists())

    def test_reference_registry_writes_default_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = FileReferenceRegistry(path=f"{tmpdir}/registry.json")
            path = registry.write_seed(registry.default_seed())
            self.assertTrue(path.exists())
            self.assertIn("KXHIGHNY", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHPHIL", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHAUS", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHDEN", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHTBOS", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHMIA", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHCHI", path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHLAX", path.read_text(encoding="utf-8"))

    def test_reference_registry_load_or_default_upgrades_old_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = FileReferenceRegistry(path=f"{tmpdir}/registry.json")
            old_payload = {
                "version": "seed_v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "stations": [
                    {
                        "station_id": "nyc-central-park",
                        "station_name": "Central Park",
                        "metar_code": "KNYC",
                        "nws_station_api_id": "KNYC",
                        "climate_product_id": "CLINYC",
                        "latitude": "40.7829",
                        "longitude": "-73.9654",
                        "timezone": "America/New_York",
                        "wfo_office": "OKX",
                        "grid_x": 33,
                        "grid_y": 37,
                        "climate_timezone_basis": "LOCAL_STANDARD_TIME",
                        "source_urls": [],
                    }
                ],
                "city_profiles": [
                    {
                        "city_id": "nyc",
                        "display_name": "New York City",
                        "station_id": "nyc-central-park",
                        "region_cluster": "northeast_metro",
                        "marine_sensitive_flag": True,
                        "onshore_wind_sectors": [80, 90],
                        "typical_peak_hour_local_by_season": {"MAM": 15},
                        "heating_window_by_season": {"MAM": [9, 17]},
                        "cloud_shock_cap_f": "5",
                        "storm_shock_cap_f": "7",
                        "marine_intrusion_cap_f": "6",
                        "wind_shift_cap_f": "4",
                        "calibration_buckets_version": "seed_v1",
                    }
                ],
                "series_definitions": [
                    {
                        "series_ticker": "KXHIGHNY",
                        "title": "Highest temperature in NYC",
                        "city_id": "nyc",
                        "category": "Climate and Weather",
                        "frequency": "daily",
                        "active_from": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                        "active_to": None,
                        "source_provenance": ["legacy_seed"],
                    }
                ],
            }
            registry.path.write_text(json.dumps(old_payload), encoding="utf-8")
            seed = registry.load_or_default()
            self.assertEqual(seed.version, FileReferenceRegistry.CURRENT_SEED_VERSION)
            self.assertEqual(len(seed.city_profiles), 8)
            self.assertEqual(len(seed.stations), 8)
            self.assertEqual(len(seed.series_definitions), 8)
            self.assertIn("KXHIGHMIA", registry.path.read_text(encoding="utf-8"))
            self.assertIn("KXHIGHCHI", registry.path.read_text(encoding="utf-8"))

    def test_derived_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DerivedAnalyticsStore(tmpdir)
            payload = {
                "provider_weights": {"NWS": "0.55"},
                "sample_sufficient": False,
            }
            path = store.write_provider_reliability("nyc", payload)
            self.assertTrue(path.exists())
            self.assertEqual(store.read_provider_reliability("nyc"), payload)

    def test_shadow_position_history_is_recorded(self) -> None:
        from decimal import Decimal

        from kalshi_weather.domain.models import ShadowPosition
        from kalshi_weather.storage import SQLiteStateStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStateStore(f"{tmpdir}/state.sqlite3")
            store.save_shadow_position(
                ShadowPosition(
                    city_id="nyc",
                    market_ticker="M1",
                    side="yes",
                    open_quantity_fp=Decimal("1"),
                    avg_cost_dollars=Decimal("0.45"),
                    cumulative_fees_dollars=Decimal("0.01"),
                    mark_pnl_dollars=Decimal("0"),
                    settled_pnl_dollars=Decimal("0"),
                    lifecycle_status="OPEN",
                )
            )
            history = store.list_shadow_position_history_payloads("nyc")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["lifecycle_status"], "OPEN")
            self.assertEqual(history[0]["side"], "yes")
            self.assertIn("recorded_at", history[0])


if __name__ == "__main__":
    unittest.main()
