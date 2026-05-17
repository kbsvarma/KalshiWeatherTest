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
    initial_sidebar_state="collapsed",
)


# Global CSS — match stocktradingbot aesthetic
st.markdown(
    """
    <style>
    /* Page bg + body */
    .stApp {
        background-color: #f7f8fa;
    }
    .main .block-container {
        padding-top: 1.5rem;
        padding-bottom: 4rem;
        max-width: 1400px;
    }
    /* Reset Streamlit defaults */
    section[data-testid="stSidebar"] { display: none; }
    [data-testid="stHeader"] { background: transparent; height: 0; }
    div[data-testid="stToolbar"] { display: none; }
    /* Containers — clean white cards with thin borders */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #ffffff;
        border: 1px solid #e5e7eb !important;
        border-radius: 10px;
        box-shadow: none !important;
    }
    /* Tabs — clean text with underline */
    button[data-baseweb="tab"] {
        font-size: 14px !important;
        font-weight: 500 !important;
        color: #6b7280 !important;
        padding: 12px 4px !important;
        margin-right: 28px !important;
    }
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #1d4ed8 !important;
    }
    div[data-baseweb="tab-highlight"] { background-color: #1d4ed8 !important; }
    div[data-baseweb="tab-border"] { background-color: #e5e7eb !important; }
    /* Dataframe cleanup */
    .stDataFrame { border: 1px solid #e5e7eb; border-radius: 8px; }
    /* Code blocks for log feed */
    .stCodeBlock { background: #f9fafb !important; border: 1px solid #e5e7eb; }
    code { font-size: 12px !important; color: #374151 !important; }
    /* Headings */
    h1, h2, h3, h4 { color: #111827 !important; font-weight: 600 !important; }
    /* Buttons */
    .stButton button {
        font-weight: 500 !important;
        font-size: 13px !important;
        border-radius: 8px !important;
    }
    /* Section header rule */
    .kxw-section-header {
        font-size: 11px;
        letter-spacing: 1.5px;
        color: #6b7280;
        font-weight: 600;
        text-transform: uppercase;
        margin: 24px 0 10px 0;
        border-bottom: 1px solid #e5e7eb;
        padding-bottom: 8px;
    }
    /* Metric cards (custom) */
    .kxw-metric {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        padding: 16px 18px;
        height: 100%;
    }
    .kxw-metric .label {
        font-size: 10px;
        letter-spacing: 1.5px;
        color: #6b7280;
        font-weight: 600;
        text-transform: uppercase;
    }
    .kxw-metric .value {
        font-size: 30px;
        font-weight: 700;
        color: #111827;
        margin-top: 8px;
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
        line-height: 1;
    }
    .kxw-metric .sub {
        font-size: 11px;
        color: #9ca3af;
        margin-top: 8px;
    }
    /* Pill badge */
    .kxw-pill {
        display: inline-block;
        padding: 3px 9px;
        border-radius: 999px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.3px;
        margin-left: 6px;
        vertical-align: middle;
    }
    .kxw-pill.live { background: #fef3c7; color: #92400e; }
    .kxw-pill.healthy { background: #ecfdf5; color: #047857; }
    .kxw-pill.down { background: #fef2f2; color: #b91c1c; }
    .kxw-pill.unknown { background: #f3f4f6; color: #4b5563; }
    .kxw-pill .dot { font-size: 10px; }
    /* Header card icon */
    .kxw-icon {
        width: 44px; height: 44px;
        background: #fef3c7;
        border-radius: 10px;
        display: flex; align-items: center; justify-content: center;
        font-size: 22px;
    }
    /* Empty state */
    .kxw-empty {
        border: 1px dashed #d1d5db;
        border-radius: 8px;
        padding: 36px;
        text-align: center;
        color: #9ca3af;
        font-size: 13px;
        background: #ffffff;
    }
    /* Top bar timestamp */
    .kxw-clock {
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
        font-size: 13px;
        color: #374151;
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 6px;
        padding: 6px 12px;
        display: inline-block;
    }
    /* Action button - check status */
    button[kind="primary"] {
        background: #111827 !important;
        color: #ffffff !important;
        border: none !important;
    }
    button[kind="primary"]:hover {
        background: #374151 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# Top bar: clock + refresh
top_l, top_r = st.columns([10, 2])
with top_r:
    sub_l, sub_r = st.columns([1, 1])
    with sub_l:
        st.markdown(
            f"<div class='kxw-clock' style='text-align:center;'>"
            f"{datetime.now(ET).strftime('%I:%M:%S %p ET')}</div>",
            unsafe_allow_html=True,
        )
    with sub_r:
        if st.button("↻ Refresh", use_container_width=True, type="primary"):
            st.cache_data.clear()
            st.rerun()

# Load data
health = load_health_status()
heartbeat = load_heartbeat()
cycles_today = load_cycle_counts_today()
metrics = load_today_metrics()
positions_df = load_open_positions()

# Compute aggregate unrealized P&L
unrealized = 0.0
if not positions_df.empty:
    for _, row in positions_df.iterrows():
        v = row.get("P&L")
        if v is not None:
            try:
                unrealized += float(v)
            except Exception:
                pass

# ── Header card ──
health_class = "healthy" if health["healthy"] else ("down" if health["summary"] == "DOWN" else "unknown")
health_dot_color = "#047857" if health["healthy"] else ("#b91c1c" if health["summary"] == "DOWN" else "#6b7280")

st.markdown(
    f"""
    <div style='background:#fff;border:1px solid #e5e7eb;border-radius:10px;
                padding:18px 22px;margin-bottom:18px;
                display:flex;align-items:center;gap:16px;'>
        <div class='kxw-icon'>🌡️</div>
        <div style='flex:1;'>
            <div style='font-size:18px;font-weight:600;color:#111827;line-height:1.2;'>
                Kalshi Weather Bot
                <span class='kxw-pill live'>LIVE</span>
                <span class='kxw-pill {health_class}'>
                    <span class='dot' style='color:{health_dot_color};'>●</span>
                    {health["summary"]}
                </span>
            </div>
            <div style='color:#6b7280;font-size:12px;margin-top:6px;'>
                18 cities · HIGH + LOW · Mac launchd · last cycle {cycles_today["last_end_et"]}
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Metric row (custom HTML cards, not st.container) ──
def _metric_html(label: str, value: str, sub: str, value_color: str | None = None) -> str:
    color_style = f"color:{value_color};" if value_color else ""
    return (
        f"<div class='kxw-metric'>"
        f"<div class='label'>{label}</div>"
        f"<div class='value' style='{color_style}'>{value}</div>"
        f"<div class='sub'>{sub}</div>"
        f"</div>"
    )

avg_spread_str = (
    f"{metrics['avg_spread_f']:.1f}"
    if metrics["avg_spread_f"] is not None else "—"
)
hb_value = heartbeat["phase"]
hb_sub = f"{heartbeat['age_seconds']}s ago" if heartbeat["age_seconds"] is not None else "no heartbeat"
pnl_color = "#047857" if unrealized >= 0 else "#b91c1c"
pnl_str = f"${unrealized:+.2f}"

cards_html = (
    "<div style='display:grid;grid-template-columns:repeat(6, 1fr);gap:14px;margin-bottom:8px;'>"
    + _metric_html("CYCLES TODAY", str(cycles_today["ended"]),
                   f"{cycles_today['started']} started")
    + _metric_html("AVG SPREAD °F", avg_spread_str, "forecast disagreement")
    + _metric_html("BOT HEARTBEAT", hb_value, hb_sub)
    + _metric_html("OPEN POSITIONS", str(metrics["open_count"]),
                   f"${metrics['deployed_usd']:.2f} deployed")
    + _metric_html("NET P&L TODAY", pnl_str, "unrealized", value_color=pnl_color)
    + _metric_html("W / L", f"{metrics['wins']}W / {metrics['losses']}L",
                   "since 2026-05-17")
    + "</div>"
)
st.markdown(cards_html, unsafe_allow_html=True)

# ── Diagnostic button ──
st.markdown("<div style='margin-top:12px;'></div>", unsafe_allow_html=True)
diag_cols = st.columns([0.22, 0.78])
with diag_cols[0]:
    diag_clicked = st.button(
        "🔍 Check Bot Status",
        type="primary",
        use_container_width=True,
        help="Runs healthcheck.sh and shows current state.",
    )
with diag_cols[1]:
    if diag_clicked:
        st.cache_data.clear()
        health = load_health_status()
        if health["healthy"]:
            st.markdown(
                f"<div style='background:#ecfdf5;border:1px solid #a7f3d0;"
                f"border-radius:8px;padding:12px 16px;color:#065f46;font-size:13px;'>"
                f"<strong>● BOT HEALTHY</strong> — {health.get('last_cycle_line', '')}"
                f"</div>",
                unsafe_allow_html=True,
            )
        elif health["summary"] == "DOWN":
            st.markdown(
                f"<div style='background:#fef2f2;border:1px solid #fecaca;"
                f"border-radius:8px;padding:12px 16px;color:#991b1b;font-size:13px;'>"
                f"<strong>● BOT DOWN</strong> — investigate immediately.<br>"
                f"<pre style='margin:8px 0 0 0;font-size:11px;color:#7f1d1d;"
                f"white-space:pre-wrap;'>{health['raw']}</pre></div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div style='background:#fffbeb;border:1px solid #fde68a;"
                f"border-radius:8px;padding:12px 16px;color:#92400e;font-size:13px;'>"
                f"<strong>● STATUS UNCLEAR</strong><br>"
                f"<pre style='margin:8px 0 0 0;font-size:11px;'>{health['raw']}</pre></div>",
                unsafe_allow_html=True,
            )

# ── Tabs ──
tab_session, tab_cities, tab_history, tab_system = st.tabs([
    "Today's Session", "Cities", "Historical", "System Health"
])

_section = lambda label: st.markdown(
    f"<div class='kxw-section-header'>{label}</div>", unsafe_allow_html=True
)
_empty = lambda msg: st.markdown(
    f"<div class='kxw-empty'>{msg}</div>", unsafe_allow_html=True
)

with tab_session:
    _section("Latest cycle summary")
    report_md = load_latest_cycle_report()
    if report_md.strip().startswith("_"):
        _empty(report_md.strip("_ "))
    else:
        st.markdown(
            f"<div style='background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
            f"padding:18px 22px;'>{report_md}</div>",
            unsafe_allow_html=True,
        )

    _section("Open positions")
    if positions_df.empty:
        _empty("No open positions today.")
    else:
        display_df = positions_df.copy()
        display_df["P&L $"] = display_df["P&L"].apply(
            lambda v: f"${float(v):+.2f}" if v is not None else "—"
        )
        display_df = display_df[
            ["Ticker", "Side", "Entry", "Current Bid", "P&L $", "Engine", "Status"]
        ]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    _section("Decision breakdown — today")
    breakdown = load_decision_breakdown_today()
    if breakdown:
        b_df = pd.DataFrame(
            sorted(breakdown.items(), key=lambda kv: -kv[1]),
            columns=["Decision", "Count"],
        )
        st.dataframe(b_df, use_container_width=True, hide_index=True, height=240)
    else:
        _empty("No decisions saved yet today.")

    _section("Live event feed (last 30)")
    events = load_live_event_feed(30)
    if events:
        st.code("\n".join(events), language=None)
    else:
        _empty("Event log empty for today. Will populate during cycles.")


with tab_cities:
    _section("Per-city overview")
    cities_df = load_city_overview()
    if cities_df.empty:
        _empty("No city data yet.")
    else:
        st.dataframe(cities_df, use_container_width=True, hide_index=True)


with tab_history:
    _section("Past daily reports")
    if REPORTS_DIR.exists():
        files = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
        if not files:
            _empty("No daily reports archived yet.")
        else:
            for fp in files[:14]:
                with st.expander(fp.stem, expanded=(fp == files[0])):
                    st.markdown(fp.read_text())
    else:
        _empty("Reports directory not found.")


with tab_system:
    _section("Launchd jobs")
    jobs = load_launchd_status()
    if jobs:
        st.dataframe(pd.DataFrame(jobs), use_container_width=True, hide_index=True)
    else:
        _empty("Could not query launchctl.")

    _section("Heartbeat file")
    st.code(heartbeat["raw"] or "(empty)", language=None)

    _section("Watchdog log tail")
    if WATCHDOG_LOG.exists():
        try:
            wd_lines = WATCHDOG_LOG.read_text().splitlines()[-20:]
            st.code("\n".join(wd_lines) if wd_lines else "(empty)", language=None)
        except Exception as exc:
            _empty(f"watchdog log read failed: {exc}")
    else:
        _empty("No watchdog log yet.")

    _section("Schema migrations applied")
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
                _empty("No migrations recorded.")
        except Exception as exc:
            _empty(f"schema_migrations query failed: {exc}")
        finally:
            conn.close()

    _section("Ollama daemon")
    try:
        ol = subprocess.run(
            ["curl", "-sf", "-m", "2", "http://localhost:11434/api/tags"],
            capture_output=True, text=True, timeout=4,
        )
        if ol.returncode == 0:
            st.markdown(
                "<div style='background:#ecfdf5;border:1px solid #a7f3d0;"
                "border-radius:8px;padding:10px 14px;color:#065f46;font-size:13px;'>"
                "<strong>● UP</strong> — AFD extraction is online</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='background:#fef2f2;border:1px solid #fecaca;"
                "border-radius:8px;padding:10px 14px;color:#991b1b;font-size:13px;'>"
                "<strong>● DOWN</strong> — AFD extraction will silently degrade</div>",
                unsafe_allow_html=True,
            )
    except Exception as exc:
        _empty(f"ollama check failed: {exc}")


# Footer note — explicit about read-only
st.markdown("---")
st.caption(
    "Read-only dashboard. Does not place orders, modify state, or control the bot. "
    f"DB connection mode: `?mode=ro`. Refresh interval: 15-60s per panel. "
    f"Rendered at {datetime.now(ET).strftime('%I:%M:%S %p ET')}."
)
