from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
MIN_PYTHON = (3, 13)
MODULE_TIMEOUT_SECONDS = 120


def _python_version_info(python_executable: str) -> Optional[Tuple[int, int]]:
    try:
        result = subprocess.run(
            [
                python_executable,
                "-c",
                "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    version_token = (result.stdout or "").strip()
    if "." not in version_token:
        return None
    major_token, minor_token = version_token.split(".", 1)
    try:
        return (int(major_token), int(minor_token))
    except ValueError:
        return None


def _candidate_python_executables() -> Sequence[str]:
    candidates: list[str] = []
    env_override = os.environ.get("KALSHI_WEATHER_PYTHON")
    if env_override:
        candidates.append(env_override)
    candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11", "python3"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)
    candidates.append("/opt/anaconda3/bin/python3")
    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


def _resolve_python_executable() -> str:
    for candidate in _candidate_python_executables():
        version = _python_version_info(candidate)
        if version is not None and version >= MIN_PYTHON:
            return candidate
    raise RuntimeError(
        "no compatible Python interpreter found; expected >= "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    )


def _run_module(module_name: str, timeout_seconds: int = MODULE_TIMEOUT_SECONDS) -> dict[str, Any]:
    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}:{existing_pythonpath}"
    python_executable = _resolve_python_executable()
    try:
        result = subprocess.run(
            [python_executable, "-m", module_name],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "module": module_name,
            "python_executable": python_executable,
            "error": {
                "returncode": None,
                "stdout": (exc.stdout or "").strip() or None,
                "stderr": f"timed out after {timeout_seconds}s",
            },
        }
    except subprocess.CalledProcessError as exc:
        return {
            "ok": False,
            "module": module_name,
            "python_executable": python_executable,
            "error": {
                "returncode": exc.returncode,
                "stdout": (exc.stdout or "").strip() or None,
                "stderr": (exc.stderr or "").strip() or None,
            },
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "raw_stdout": result.stdout,
        }
    return {
        "ok": True,
        "module": module_name,
        "python_executable": python_executable,
        "payload": payload,
    }


def _payload_value(result: dict[str, Any], *path: str) -> Any:
    if not result.get("ok"):
        return None
    current = result.get("payload")
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _payload_city_reports(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not result.get("ok"):
        return {}
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return {}
    city_reports = payload.get("city_reports")
    if isinstance(city_reports, dict):
        return {
            str(city_id): report
            for city_id, report in city_reports.items()
            if isinstance(report, dict)
        }
    if isinstance(city_reports, list):
        reports_by_city: dict[str, dict[str, Any]] = {}
        for item in city_reports:
            if not isinstance(item, dict):
                continue
            city_id = item.get("city_id")
            if city_id:
                reports_by_city[str(city_id)] = item
        return reports_by_city
    return {}


def main() -> None:
    backfill_observations = _run_module("kalshi_weather.tools.backfill_observations")
    backfill_cli = _run_module("kalshi_weather.tools.backfill_cli_archive")
    calibration = _run_module("kalshi_weather.tools.update_forecast_calibration")
    settlement = _run_module("kalshi_weather.tools.validate_settlement_corpus")
    nowcast = _run_module("kalshi_weather.tools.validate_nowcast_bridge")
    cycle = _run_module("kalshi_weather.tools.run_city_cycle")
    reconcile = _run_module("kalshi_weather.tools.reconcile_shadow_positions")
    qualification = _run_module("kalshi_weather.tools.update_qualification")
    shadow = _run_module("kalshi_weather.tools.shadow_report")
    opportunities = _run_module("kalshi_weather.tools.survey_opportunities")

    city_reports: dict[str, dict[str, Any]] = {}
    report_groups = {
        "backfill_observations": _payload_city_reports(backfill_observations),
        "backfill_cli_archive": _payload_city_reports(backfill_cli),
        "forecast_calibration": _payload_city_reports(calibration),
        "settlement_validation": _payload_city_reports(settlement),
        "nowcast_validation": _payload_city_reports(nowcast),
        "decision_cycle": _payload_city_reports(cycle),
        "shadow_reconciliation": _payload_city_reports(reconcile),
        "qualification": _payload_city_reports(qualification),
        "shadow_report": _payload_city_reports(shadow),
        "opportunity_survey": _payload_city_reports(opportunities),
    }
    for module_name, reports in report_groups.items():
        for city_id, report in reports.items():
            city_reports.setdefault(city_id, {})
            city_reports[city_id][module_name] = report

    output = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "python_executable": _resolve_python_executable(),
        "backfill_observations": {
            "ok": backfill_observations.get("ok"),
            "saved_observation_count": _payload_value(backfill_observations, "saved_observation_count"),
            "latest_event_time": _payload_value(backfill_observations, "latest_event_time"),
            "error": backfill_observations.get("error"),
        },
        "backfill_cli_archive": {
            "ok": backfill_cli.get("ok"),
            "stored_report_count": _payload_value(backfill_cli, "stored_report_count"),
            "settlement_day_count": _payload_value(backfill_cli, "settlement_day_count"),
            "error": backfill_cli.get("error"),
        },
        "forecast_calibration": {
            "ok": calibration.get("ok"),
            "city_reports": _payload_city_reports(calibration),
            "error": calibration.get("error"),
        },
        "settlement_validation": {
            "ok": settlement.get("ok"),
            "summary": _payload_value(settlement, "summary"),
            "city_reports": _payload_city_reports(settlement),
            "error": settlement.get("error"),
        },
        "nowcast_validation": {
            "ok": nowcast.get("ok"),
            "city_reports": _payload_city_reports(nowcast),
            "error": nowcast.get("error"),
        },
        "decision_cycle": {
            "ok": cycle.get("ok"),
            "decision_count": _payload_value(cycle, "decision_count"),
            "city_reports": _payload_city_reports(cycle),
            "error": cycle.get("error"),
        },
        "shadow_reconciliation": {
            "ok": reconcile.get("ok"),
            "city_reports": _payload_city_reports(reconcile),
            "error": reconcile.get("error"),
        },
        "qualification": {
            "ok": qualification.get("ok"),
            "city_reports": _payload_city_reports(qualification),
            "error": qualification.get("error"),
        },
        "live_gating": {
            "ok": shadow.get("ok"),
            "city_reports": _payload_city_reports(shadow),
            "error": shadow.get("error"),
        },
        "opportunity_survey": {
            "ok": opportunities.get("ok"),
            "city_reports": _payload_city_reports(opportunities),
            "opportunity_board": _payload_value(opportunities, "opportunity_board"),
            "error": opportunities.get("error"),
        },
        "city_reports": city_reports,
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
