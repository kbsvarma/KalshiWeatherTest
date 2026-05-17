from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from kalshi_weather.ingestion.contracts import TimingDriftMonitor


class TimingDriftMonitorTest(unittest.TestCase):
    def test_timing_drift_average_and_threshold(self) -> None:
        monitor = TimingDriftMonitor()
        now = datetime.now(timezone.utc)
        monitor.record("nws_obs", now - timedelta(minutes=5), now)
        monitor.record("nws_obs", now - timedelta(minutes=15), now)

        average = monitor.average("nws_obs")
        self.assertIsNotNone(average)
        self.assertGreaterEqual(average, 600)
        self.assertTrue(monitor.should_widen_uncertainty("nws_obs", warning_seconds=600))


if __name__ == "__main__":
    unittest.main()
