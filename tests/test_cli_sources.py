from __future__ import annotations

import unittest

from kalshi_weather.storage import FileReferenceRegistry
from kalshi_weather.tools.backfill_cli_archive import _report_matches_station


class CliSourceMatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        registry = FileReferenceRegistry()
        seed = registry.default_seed()
        contexts = registry.context_by_city_id(seed)
        self.nyc_station = contexts["nyc"].station
        self.chi_station = contexts["chi"].station
        self.lax_station = contexts["lax"].station

    def test_matches_chicago_midway_report(self) -> None:
        self.assertTrue(
            _report_matches_station(
                "...THE CHICAGO MIDWAY IL CLIMATE SUMMARY FOR APRIL 7 2026...",
                self.chi_station,
            )
        )

    def test_matches_lax_report(self) -> None:
        self.assertTrue(
            _report_matches_station(
                "...THE LOS ANGELES AIRPORT CA CLIMATE SUMMARY FOR APRIL 5 2026...",
                self.lax_station,
            )
        )

    def test_matches_central_park_report(self) -> None:
        self.assertTrue(
            _report_matches_station(
                "...THE CENTRAL PARK NY CLIMATE SUMMARY FOR APRIL 5 2026...",
                self.nyc_station,
            )
        )


if __name__ == "__main__":
    unittest.main()
