from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from kalshi_weather.tools.refresh_empirical_blockers import (
    _payload_city_reports,
    _payload_value,
    _resolve_python_executable,
    _run_module,
)


class RefreshEmpiricalBlockersTest(unittest.TestCase):
    def test_run_module_returns_payload_on_success(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python3", "-m", "demo"],
            returncode=0,
            stdout=json.dumps({"value": 1}),
            stderr="",
        )
        with patch("kalshi_weather.tools.refresh_empirical_blockers._resolve_python_executable", return_value="/tmp/python3.13"), patch(
            "kalshi_weather.tools.refresh_empirical_blockers.subprocess.run",
            return_value=completed,
        ):
            result = _run_module("demo.module")
        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["value"], 1)
        self.assertEqual(result["python_executable"], "/tmp/python3.13")

    def test_run_module_returns_structured_error_on_failure(self) -> None:
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["python3", "-m", "demo"],
            output="",
            stderr="network failed",
        )
        with patch("kalshi_weather.tools.refresh_empirical_blockers._resolve_python_executable", return_value="/tmp/python3.13"), patch(
            "kalshi_weather.tools.refresh_empirical_blockers.subprocess.run",
            side_effect=error,
        ):
            result = _run_module("demo.module")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["returncode"], 1)
        self.assertEqual(result["error"]["stderr"], "network failed")
        self.assertEqual(result["python_executable"], "/tmp/python3.13")

    def test_run_module_returns_structured_error_on_timeout(self) -> None:
        error = subprocess.TimeoutExpired(cmd=["python3", "-m", "demo"], timeout=5)
        with patch(
            "kalshi_weather.tools.refresh_empirical_blockers._resolve_python_executable",
            return_value="/tmp/python3.13",
        ), patch(
            "kalshi_weather.tools.refresh_empirical_blockers.subprocess.run",
            side_effect=error,
        ):
            result = _run_module("demo.module", timeout_seconds=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["stderr"], "timed out after 5s")

    def test_payload_value_returns_none_on_failed_run(self) -> None:
        self.assertIsNone(_payload_value({"ok": False, "error": {}}, "nested"))

    def test_payload_city_reports_handles_dict_and_list_shapes(self) -> None:
        self.assertEqual(
            _payload_city_reports(
                {
                    "ok": True,
                    "payload": {"city_reports": {"nyc": {"decision_count": 1}}},
                }
            ),
            {"nyc": {"decision_count": 1}},
        )
        self.assertEqual(
            _payload_city_reports(
                {
                    "ok": True,
                    "payload": {
                        "city_reports": [
                            {"city_id": "nyc", "decision_count": 1},
                            {"city_id": "chi", "decision_count": 0},
                        ]
                    },
                }
            ),
            {
                "nyc": {"city_id": "nyc", "decision_count": 1},
                "chi": {"city_id": "chi", "decision_count": 0},
            },
        )

    def test_resolve_python_executable_uses_first_compatible_candidate(self) -> None:
        with patch(
            "kalshi_weather.tools.refresh_empirical_blockers._candidate_python_executables",
            return_value=("/usr/bin/python3", "/opt/anaconda3/bin/python3"),
        ), patch(
            "kalshi_weather.tools.refresh_empirical_blockers._python_version_info",
            side_effect=[(3, 9), (3, 13)],
        ):
            resolved = _resolve_python_executable()
        self.assertEqual(resolved, "/opt/anaconda3/bin/python3")


if __name__ == "__main__":
    unittest.main()
