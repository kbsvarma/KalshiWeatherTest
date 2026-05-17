"""Kalshi Weather Bot — read-only Streamlit dashboard.

DESIGN CONSTRAINTS (do not violate):
  - READ-ONLY. SQLite is opened with `mode=ro`; no INSERT/UPDATE/DELETE
    anywhere in this file.
  - This is a VIEWING LAYER. It does NOT import from src/kalshi_weather,
    does NOT modify any bot files, does NOT control launchd jobs.
  - Failures here must never affect the bot. Each panel renders
    independently; one broken query does not break the page.

Run: PYTHONPATH=src streamlit run scripts/dashboard.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st


# ── Paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "state" / "runtime.sqlite3"
LOGS = ROOT / "logs"
CRON_LOG = LOGS / "cron_cycle.log"
WATCHDOG_LOG = LOGS / "watchdog.log"
HEARTBEAT_FILE = LOGS / "heartbeat.txt"
HEALTHCHECK = ROOT / "scripts" / "healthcheck.sh"
REPORTS_DIR = LOGS / "cycle_reports"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


# ── Read-only DB connection helper ───────────────────────────────────────
def _ro_connect() -> sqlite3.Connection | None:
    """Return a read-only SQLite connection, or None if DB missing."""
    if not DB_PATH.exists():
        return None
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _today_et_iso() -> str:
    return datetime.now(ET).date().isoformat()


def _utc_to_et(utc_iso: str) -> str:
    """Convert UTC ISO timestamp to ET HH:MM:SS AM/PM for display."""
    try:
        s = utc_iso.rstrip("Z")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(ET).strftime("%I:%M:%S %p")
    except Exception:
        return "—"


# ── Data loaders (each cached lightly to avoid hammering DB) ─────────────
@st.cache_data(ttl=15)
def load_health_status() -> dict:
    """Run healthcheck.sh and parse its output. Cached briefly."""
    if not HEALTHCHECK.exists():
        return {"healthy": False, "raw": "healthcheck.sh missing", "summary": "UNKNOWN"}
    try:
        result = subprocess.run(
            ["/bin/zsh", str(HEALTHCHECK)],
            capture_output=True, text=True, timeout=10,
        )
        out = (result.stdout or "") + (result.stderr or "")
    except Exception as exc:
        return {"healthy": False, "raw": f"healthcheck failed: {exc}", "summary": "ERROR"}
    healthy = "BOT HEALTHY" in out
    down = "BOT DOWN" in out
    summary = "HEALTHY" if healthy else ("DOWN" if down else "UNKNOWN")
    last_cycle = ""
    for line in out.splitlines():
        if "LAST CYCLE" in line:
            last_cycle = line.strip()
            break
    return {"healthy": healthy, "raw": out, "summary": summary, "last_cycle_line": last_cycle}


@st.cache_data(ttl=15)
def load_heartbeat() -> dict:
    if not HEARTBEAT_FILE.exists():
        return {"phase": "unknown", "age_seconds": None, "raw": "(no heartbeat file)"}
    try:
        text = HEARTBEAT_FILE.read_text()
        mtime = HEARTBEAT_FILE.stat().st_mtime
        age = int(datetime.now(UTC).timestamp() - mtime)
        phase = "unknown"
        for line in text.splitlines():
            if line.startswith("phase="):
                phase = line.split("=", 1)[1].strip()
        return {"phase": phase, "age_seconds": age, "raw": text.strip()}
    except Exception as exc:
        return {"phase": "error", "age_seconds": None, "raw": str(exc)}


@st.cache_data(ttl=20)
def load_cycle_counts_today() -> dict:
    """Parse cron_cycle.log to count today's completed cycles."""
    if not CRON_LOG.exists():
        return {"started": 0, "ended": 0, "last_end_et": "—"}
    started = ended = 0
    last_end_utc = ""
    today_utc_prefix = datetime.now(UTC).date().isoformat()
    today_et_date = datetime.now(ET).date()
    try:
        with CRON_LOG.open() as f:
            for line in f:
                # bracketed UTC stamp at start
                if "cycle start" in line and today_utc_prefix in line:
                    started += 1
                if "cycle end" in line and today_utc_prefix in line:
                    ended += 1
                    last_end_utc = line.split("]")[0].strip("[")
    except Exception:
        return {"started": 0, "ended": 0, "last_end_et": "—"}
    return {
        "started": started,
        "ended": ended,
        "last_end_et": _utc_to_et(last_end_utc) if last_end_utc else "—",
        "last_end_utc": last_end_utc,
    }


@st.cache_data(ttl=30)
def load_open_positions() -> pd.DataFrame:
    """Return today's PLACED live orders joined to latest orderbook for P&L."""
    conn = _ro_connect()
    if conn is None:
        return pd.DataFrame()
    try:
        live_rows = conn.execute(
            "SELECT market_ticker, payload_json, status, created_at "
            "FROM live_orders WHERE status='PLACED_EXECUTED' "
            "AND substr(created_at, 1, 10) = ? "
            "ORDER BY created_at",
            (_today_et_iso(),),
        ).fetchall()
        positions = []
        for ticker, payload_raw, status, created_at in live_rows:
            try:
                payload = json.loads(payload_raw)
            except Exception:
                continue
            yes_p = payload.get("yes_price")
            no_p = payload.get("no_price")
            entry_cents = int(yes_p) if yes_p is not None else (int(no_p) if no_p is not None else 0)
            side = "yes" if yes_p is not None else "no"
            if entry_cents <= 0:
                continue
            entry = entry_cents / 100.0
            # Top opposite-side bid from latest orderbook
            ob_row = conn.execute(
                "SELECT payload_json FROM orderbook_snapshots "
                "WHERE market_ticker = ? ORDER BY as_of_time DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            current_bid = None
            if ob_row:
                try:
                    ob = json.loads(ob_row[0])
                    bids = ob.get("yes_bids_ladder" if side == "yes" else "no_bids_ladder") or []
                    if bids:
                        current_bid = max(float(p) for p, _ in bids)
                except Exception:
                    pass
            pnl = (current_bid - entry) if current_bid is not None else None
            # Latest decision for this ticker
            dec_row = conn.execute(
                "SELECT payload_json FROM decisions "
                "WHERE market_ticker = ? ORDER BY as_of_time DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            decision = "—"
            if dec_row:
                try:
                    dec_payload = json.loads(dec_row[0])
                    decision = dec_payload.get("final_decision", "—")
                except Exception:
                    pass
            positions.append({
                "Ticker": ticker,
                "Side": side.upper(),
                "Entry": f"${entry:.2f}",
                "Current Bid": f"${current_bid:.2f}" if current_bid is not None else "—",
                "P&L": pnl,
                "Engine": decision,
                "Status": ("—" if pnl is None
                           else ("PROFIT" if pnl >= 0 else "UNDERWATER")),
            })
        return pd.DataFrame(positions)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=30)
def load_today_metrics() -> dict:
    """Aggregate metrics for the header cards."""
    conn = _ro_connect()
    out = {
        "avg_spread_f": None,
        "open_count": 0,
        "deployed_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
    }
    if conn is None:
        return out
    try:
        today = _today_et_iso()
        # avg spread from decisions today
        rows = conn.execute(
            "SELECT json_extract(payload_json, '$.forecast_summary.provider_spread_f') "
            "FROM decisions WHERE substr(as_of_time, 1, 10) = ? "
            "AND json_extract(payload_json, '$.forecast_summary.provider_spread_f') IS NOT NULL",
            (today,),
        ).fetchall()
        spreads = [float(r[0]) for r in rows if r[0] is not None]
        if spreads:
            out["avg_spread_f"] = statistics.median(spreads)
        # open + deployed
        live = conn.execute(
            "SELECT payload_json FROM live_orders WHERE status='PLACED_EXECUTED' "
            "AND substr(created_at, 1, 10) = ?",
            (today,),
        ).fetchall()
        out["open_count"] = len(live)
        for (raw,) in live:
            try:
                p = json.loads(raw)
                yes_p = p.get("yes_price")
                no_p = p.get("no_price")
                cents = int(yes_p) if yes_p is not None else (int(no_p) if no_p is not None else 0)
                out["deployed_usd"] += cents / 100.0
            except Exception:
                continue
        # W/L since 2026-05-17 — count settled fills with positive/negative pnl
        # (very simple — full reconciliation would join shadow_fills to settlements)
        # For v1, return 0/0 if no settled bets yet — accurate.
        return out
    except Exception:
        return out
    finally:
        conn.close()


@st.cache_data(ttl=20)
def load_decision_breakdown_today() -> dict:
    conn = _ro_connect()
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT json_extract(payload_json, '$.final_decision') AS fd, COUNT(*) "
            "FROM decisions WHERE substr(as_of_time, 1, 10) = ? GROUP BY fd",
            (_today_et_iso(),),
        ).fetchall()
        return {str(fd): int(n) for fd, n in rows if fd is not None}
    except Exception:
        return {}
    finally:
        conn.close()


def load_latest_cycle_report() -> str:
    """Return the daily Markdown report content or a fallback message."""
    today = _today_et_iso()
    md_path = REPORTS_DIR / f"{today}.md"
    if md_path.exists():
        try:
            return md_path.read_text()
        except Exception as exc:
            return f"_Failed to read report: {exc}_"
    return "_No cycle report file yet for today. The first cycle creates it._"


def load_live_event_feed(limit: int = 30) -> list[str]:
    """Tail of cron_cycle.log filtered to interesting lines."""
    if not CRON_LOG.exists():
        return []
    interesting = re.compile(
        r"\[LIVE-EXEC\]|\[EXIT-EXEC\]|\[AFD\]|\[SPC\]|\[GOES-PROXY\]|\[SOIL\]|"
        r"\[PROVIDER-WEIGHTS\]|cycle start|cycle end|FAILED|WARN|ERROR"
    )
    matches: list[str] = []
    try:
        # tail efficiently — read last ~200KB
        size = CRON_LOG.stat().st_size
        with CRON_LOG.open() as f:
            if size > 200_000:
                f.seek(size - 200_000)
                f.readline()  # discard partial line
            for line in f:
                if interesting.search(line):
                    matches.append(line.rstrip())
    except Exception:
        return []
    return matches[-limit:]


@st.cache_data(ttl=60)
def load_city_overview() -> pd.DataFrame:
    """One row per city with qualification + recent activity + top providers."""
    conn = _ro_connect()
    if conn is None:
        return pd.DataFrame()
    try:
        # Qualification states
        q_rows = conn.execute(
            "SELECT city_id, state FROM qualification_states ORDER BY city_id"
        ).fetchall()
        # Decisions today by city
        d_rows = conn.execute(
            "SELECT city_id, json_extract(payload_json, '$.final_decision'), COUNT(*) "
            "FROM decisions WHERE substr(as_of_time, 1, 10) = ? "
            "GROUP BY city_id, json_extract(payload_json, '$.final_decision')",
            (_today_et_iso(),),
        ).fetchall()
        by_city: dict[str, dict[str, int]] = {}
        for city_id, fd, n in d_rows:
            by_city.setdefault(city_id, {})[str(fd)] = int(n)
        # Per-city best provider (lowest mean abs_error_f) from provider_errors
        p_rows = conn.execute(
            "SELECT city_id, provider_id, AVG(abs_error_f) AS mae, COUNT(*) AS n "
            "FROM provider_errors GROUP BY city_id, provider_id"
        ).fetchall()
        best_by_city: dict[str, tuple[str, float]] = {}
        for city_id, provider, mae, n in p_rows:
            try:
                mae_f = float(mae)
            except Exception:
                continue
            existing = best_by_city.get(city_id)
            if existing is None or mae_f < existing[1]:
                best_by_city[city_id] = (str(provider), mae_f)
        # Open positions count by city — parse ticker → city via decisions
        op_rows = conn.execute(
            "SELECT json_extract(payload_json, '$.market_ticker'), "
            "json_extract(payload_json, '$.ticker') "
            "FROM live_orders WHERE status='PLACED_EXECUTED' "
            "AND substr(created_at, 1, 10) = ?",
            (_today_et_iso(),),
        ).fetchall()
        # We need ticker → city. Decision payloads carry city_id.
        open_by_city: dict[str, int] = {}
        for (mt, _t2) in op_rows:
            ticker = mt or _t2
            if not ticker:
                continue
            dec_row = conn.execute(
                "SELECT city_id FROM decisions WHERE market_ticker = ? LIMIT 1",
                (ticker,),
            ).fetchone()
            if dec_row:
                cid = dec_row[0]
                open_by_city[cid] = open_by_city.get(cid, 0) + 1
        # Build dataframe
        rows = []
        for city_id, state in q_rows:
            decs = by_city.get(city_id, {})
            best = best_by_city.get(city_id)
            rows.append({
                "City": city_id,
                "Qualification": state,
                "Open Positions": open_by_city.get(city_id, 0),
                "WATCH": decs.get("WATCH", 0),
                "TAKER_ALLOWED": decs.get("TAKER_ALLOWED", 0),
                "EXIT/REDUCE": decs.get("EXIT", 0) + decs.get("REDUCE", 0),
                "Best Provider": best[0] if best else "—",
                "Best MAE °F": round(best[1], 2) if best else None,
            })
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def load_launchd_status() -> list[dict]:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        if "kalshi.weather" not in line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({
                "PID": parts[0],
                "Exit": parts[1],
                "Label": parts[2],
            })
    return rows


# ── Streamlit page ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="Kalshi Weather Bot",
    page_icon="🌡️",
    layout="wide",
)

# Top right: timestamp + manual refresh
top_cols = st.columns([8, 1, 1])
with top_cols[1]:
    st.markdown(
        f"<div style='text-align:right;padding-top:8px;'>"
        f"<code>{datetime.now(ET).strftime('%I:%M:%S %p ET')}</code></div>",
        unsafe_allow_html=True,
    )
with top_cols[2]:
    if st.button("↻ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Header card ──
health = load_health_status()
heartbeat = load_heartbeat()

dot_color = "#16a34a" if health["healthy"] else "#dc2626"
status_label = health["summary"]

cycles_today = load_cycle_counts_today()
metrics = load_today_metrics()

with st.container(border=True):
    h_cols = st.columns([0.1, 0.6, 0.3])
    with h_cols[0]:
        st.markdown(
            "<div style='font-size:42px;text-align:center;'>🌡️</div>",
            unsafe_allow_html=True,
        )
    with h_cols[1]:
        st.markdown(
            f"<h3 style='margin:0;'>Kalshi Weather Bot "
            f"<span style='background:#fef3c7;color:#92400e;padding:2px 8px;"
            f"border-radius:6px;font-size:12px;font-weight:600;margin-left:8px;'>"
            f"LIVE</span> "
            f"<span style='background:#f3f4f6;color:{dot_color};padding:2px 8px;"
            f"border-radius:6px;font-size:12px;font-weight:600;margin-left:4px;'>"
            f"● {status_label}</span></h3>"
            f"<div style='color:#6b7280;font-size:13px;margin-top:4px;'>"
            f"18 cities · HIGH + LOW · Mac launchd · "
            f"last cycle {cycles_today['last_end_et']}</div>",
            unsafe_allow_html=True,
        )

# ── Metric row ──
st.markdown("&nbsp;")  # spacing
m_cols = st.columns(6)
def _metric_card(col, label: str, value: str, sub: str = "") -> None:
    with col:
        with st.container(border=True):
            st.markdown(
                f"<div style='color:#6b7280;font-size:10px;letter-spacing:1px;"
                f"font-weight:600;'>{label}</div>"
                f"<div style='font-size:28px;font-weight:700;margin-top:6px;"
                f"font-family:monospace;'>{value}</div>"
                f"<div style='color:#6b7280;font-size:11px;margin-top:4px;'>{sub}</div>",
                unsafe_allow_html=True,
            )

_metric_card(m_cols[0], "CYCLES TODAY", str(cycles_today["ended"]),
             f"{cycles_today['started']} started")
_metric_card(m_cols[1], "AVG SPREAD °F",
             f"{metrics['avg_spread_f']:.1f}" if metrics['avg_spread_f'] is not None else "—",
             "forecast disagreement")
hb_phase = heartbeat["phase"]
hb_age = heartbeat["age_seconds"]
hb_value = hb_phase
hb_sub = f"{hb_age}s ago" if hb_age is not None else "no heartbeat"
_metric_card(m_cols[2], "BOT HEARTBEAT", hb_value, hb_sub)
_metric_card(m_cols[3], "OPEN POSITIONS", str(metrics["open_count"]),
             f"${metrics['deployed_usd']:.2f} deployed")
# Compute aggregate unrealized P&L from open positions
positions_df = load_open_positions()
unrealized = 0.0
if not positions_df.empty:
    for _, row in positions_df.iterrows():
        v = row.get("P&L")
        if v is not None:
            try:
                unrealized += float(v)
            except Exception:
                pass
pnl_color = "#16a34a" if unrealized >= 0 else "#dc2626"
_metric_card(
    m_cols[4], "NET P&L TODAY",
    f"<span style='color:{pnl_color}'>${unrealized:+.2f}</span>",
    "unrealized",
)
_metric_card(m_cols[5], "W / L", f"{metrics['wins']}W / {metrics['losses']}L",
             "since 2026-05-17")

# ── Diagnostic button ──
st.markdown("&nbsp;")
diag_cols = st.columns([0.25, 0.75])
with diag_cols[0]:
    diag_clicked = st.button(
        "🔍 Check Bot Status",
        type="primary",
        use_container_width=True,
        help="Runs healthcheck.sh and shows current state.",
    )
with diag_cols[1]:
    if diag_clicked:
        # Force-refresh health data
        st.cache_data.clear()
        health = load_health_status()
        if health["healthy"]:
            st.success(
                f"**BOT HEALTHY** — {health.get('last_cycle_line', '')}",
                icon="✅",
            )
        elif health["summary"] == "DOWN":
            st.error(
                f"**BOT DOWN** — investigate immediately.\n\n"
                f"```\n{health['raw']}\n```",
                icon="🚨",
            )
        else:
            st.warning(
                f"**STATUS UNCLEAR**\n\n```\n{health['raw']}\n```",
                icon="⚠️",
            )

# ── Tabs ──
tab_session, tab_cities, tab_history, tab_system = st.tabs([
    "Today's Session", "Cities", "Historical", "System Health"
])

with tab_session:
    st.markdown("### Latest Cycle Summary")
    report_md = load_latest_cycle_report()
    with st.container(border=True):
        st.markdown(report_md)

    st.markdown("### Open Positions")
    if positions_df.empty:
        st.info("No open positions today.")
    else:
        # Color-code P&L
        def _color_pnl(val):
            try:
                v = float(val)
                if v > 0:
                    return f"color: #16a34a; font-weight: 600;"
                if v < 0:
                    return f"color: #dc2626; font-weight: 600;"
            except Exception:
                pass
            return ""
        display_df = positions_df.copy()
        display_df["P&L $"] = display_df["P&L"].apply(
            lambda v: f"${float(v):+.2f}" if v is not None else "—"
        )
        display_df = display_df[
            ["Ticker", "Side", "Entry", "Current Bid", "P&L $", "Engine", "Status"]
        ]
        st.dataframe(
            display_df, use_container_width=True, hide_index=True,
        )

    st.markdown("### Decision Breakdown — Today")
    breakdown = load_decision_breakdown_today()
    if breakdown:
        b_df = pd.DataFrame(
            sorted(breakdown.items(), key=lambda kv: -kv[1]),
            columns=["Decision", "Count"],
        )
        st.dataframe(b_df, use_container_width=True, hide_index=True, height=200)
    else:
        st.info("No decisions saved yet today.")

    st.markdown("### Live Event Feed (last 30 interesting lines)")
    events = load_live_event_feed(30)
    if events:
        with st.container(border=True):
            st.code("\n".join(events), language=None)
    else:
        st.info("No log activity yet.")


with tab_cities:
    st.markdown("### Per-City Overview")
    cities_df = load_city_overview()
    if cities_df.empty:
        st.info("No city data yet.")
    else:
        st.dataframe(cities_df, use_container_width=True, hide_index=True)


with tab_history:
    st.markdown("### Past Daily Reports")
    if REPORTS_DIR.exists():
        files = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
        if not files:
            st.info("No daily reports archived yet.")
        else:
            for fp in files[:14]:
                with st.expander(fp.stem, expanded=(fp == files[0])):
                    st.markdown(fp.read_text())
    else:
        st.info("Reports directory not found.")


with tab_system:
    st.markdown("### launchd Jobs")
    jobs = load_launchd_status()
    if jobs:
        st.dataframe(pd.DataFrame(jobs), use_container_width=True, hide_index=True)
    else:
        st.warning("Could not query launchctl.")

    st.markdown("### Heartbeat File")
    with st.container(border=True):
        st.code(heartbeat["raw"], language=None)

    st.markdown("### Watchdog Log Tail")
    if WATCHDOG_LOG.exists():
        try:
            wd_lines = WATCHDOG_LOG.read_text().splitlines()[-20:]
            with st.container(border=True):
                st.code("\n".join(wd_lines) if wd_lines else "(empty)", language=None)
        except Exception as exc:
            st.error(f"watchdog log read failed: {exc}")
    else:
        st.info("No watchdog log yet.")

    st.markdown("### Schema Migrations Applied")
    conn = _ro_connect()
    if conn:
        try:
            mig_rows = conn.execute(
                "SELECT migration_id, applied_at, description "
                "FROM schema_migrations ORDER BY applied_at"
            ).fetchall()
            if mig_rows:
                mig_df = pd.DataFrame(
                    mig_rows, columns=["ID", "Applied At (UTC)", "Description"]
                )
                st.dataframe(mig_df, use_container_width=True, hide_index=True)
            else:
                st.info("No migrations recorded.")
        except Exception as exc:
            st.warning(f"schema_migrations query failed: {exc}")
        finally:
            conn.close()

    st.markdown("### Ollama Daemon")
    try:
        ol = subprocess.run(
            ["curl", "-sf", "-m", "2", "http://localhost:11434/api/tags"],
            capture_output=True, text=True, timeout=4,
        )
        if ol.returncode == 0:
            st.success("Ollama daemon: UP", icon="✅")
        else:
            st.error("Ollama daemon: DOWN (AFD extraction will silently degrade)", icon="🚨")
    except Exception as exc:
        st.warning(f"ollama check failed: {exc}")


# Footer note — explicit about read-only
st.markdown("---")
st.caption(
    "Read-only dashboard. Does not place orders, modify state, or control the bot. "
    f"DB connection mode: `?mode=ro`. Refresh interval: 15-60s per panel. "
    f"Rendered at {datetime.now(ET).strftime('%I:%M:%S %p ET')}."
)
