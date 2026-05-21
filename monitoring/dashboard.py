#!/usr/bin/env python3
"""
monitoring/dashboard.py — Streamlit dashboard for the AI trading bot.

Run with:
    streamlit run monitoring/dashboard.py

Pages:
    1  Live Overview       — account summary, watchlist, bot status
    2  Open Positions      — live positions with P&L and close buttons
    3  Trade History       — trade log, cumulative P&L, performance metrics
    4  Bot Decisions       — session log browser with full agent reasoning
    5  Performance         — equity curve, drawdown, monthly heatmap, scatter
    6  Settings & Controls — kill switch, watchlist, risk params, manual trigger
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv, set_key

# ── Path bootstrap ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

# ── Constants ─────────────────────────────────────────────────────────────────
LOGS_DIR    = _ROOT / "logs"
KILL_SWITCH = _ROOT / "kill_switch.lock"
ENV_FILE    = _ROOT / ".env"
TRADES_CSV  = LOGS_DIR / "trades.csv"

_SGT = ZoneInfo("Asia/Singapore")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("dashboard")


# All scheduled jobs in (hour, minute, display_label) order.
# Keep in sync with scheduler.py if the schedule changes.
_SCHEDULE = [
    (8,  50, "Reset counters"),
    (9,   0, "SGX open analysis"),
    (15,  0, "SGX close analysis"),
    (21, 30, "NYSE open analysis"),
]


def _next_scheduled_run() -> tuple:
    """Return (next_datetime_sgt, label) for the next upcoming scheduled job."""
    now = datetime.now(_SGT)
    for day_offset in range(8):
        day = now + timedelta(days=day_offset)
        if day.weekday() >= 5:   # skip Saturday (5) and Sunday (6)
            continue
        for hour, minute, label in _SCHEDULE:
            t = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if t > now:
                return t, label
    # Fallback: next Monday 08:50 (should never be reached within 8 days)
    days_ahead = (7 - now.weekday()) % 7 or 7
    t = (now + timedelta(days=days_ahead)).replace(
        hour=8, minute=50, second=0, microsecond=0
    )
    return t, "Reset counters"


def _iso_to_sgt(ts_str: str, fmt: str = "%Y-%m-%d %H:%M SGT") -> str:
    """Parse an ISO UTC timestamp string and return it formatted in SGT."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_SGT).strftime(fmt)
    except Exception:
        return ts_str[:16].replace("T", " ")

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="AI Trading Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.tag {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 0.82em;
    font-weight: 600;
    letter-spacing: 0.03em;
}
.tag-approved { background: #0d4a2e; color: #00c46a; }
.tag-blocked  { background: #4a0d0d; color: #ff4b4b; }
.tag-hold     { background: #3a3000; color: #ffa040; }
.tag-buy      { background: #0d4a2e; color: #00c46a; }
.tag-sell     { background: #4a0d0d; color: #ff4b4b; }
.pos-pnl      { color: #00c46a; font-weight: 700; }
.neg-pnl      { color: #ff4b4b; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Executor / broker helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource
def _make_executor():
    """Create the configured broker connector once per dashboard process."""
    broker = os.getenv("BROKER", "moomoo").lower()
    if broker == "moomoo":
        try:
            from execution.moomoo import MooMooConnector
            return MooMooConnector()
        except Exception as exc:
            logger.warning("MooMooConnector init failed: %s", exc)
            return None
    else:
        api_key = os.getenv("ALPACA_API_KEY", "")
        sec_key = os.getenv("ALPACA_SECRET_KEY", "")
        if not api_key or api_key in ("", "your-key-here"):
            return None
        if not sec_key or sec_key in ("", "your-key-here"):
            return None
        try:
            from execution.alpaca import AlpacaExecutor
            return AlpacaExecutor()
        except Exception as exc:
            logger.warning("AlpacaExecutor init failed: %s", exc)
            return None


def _executor():
    return _make_executor()


def _get_account() -> Optional[dict]:
    ex = _executor()
    if ex is None:
        return None
    try:
        if hasattr(ex, "get_account_balance"):
            bal = ex.get_account_balance()
            pv  = bal.get("portfolio_value", 0.0)
            return {
                "equity":         pv,
                "cash":           bal.get("cash", 0.0),
                "buying_power":   bal.get("cash", 0.0),
                "daily_pnl":      0.0,
                "unrealised_pnl": bal.get("market_value", 0.0),
            }
        return ex.get_account()
    except Exception as exc:
        logger.warning("get_account failed: %s", exc)
        return None


def _get_positions() -> list:
    ex = _executor()
    if ex is None:
        return []
    try:
        return ex.get_positions()
    except Exception as exc:
        logger.warning("get_positions failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Data loaders
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=120)
def _load_trades() -> pd.DataFrame:
    if not TRADES_CSV.exists():
        return pd.DataFrame(
            columns=["timestamp", "ticker", "action", "quantity", "price", "order_id"]
        )
    try:
        df = pd.read_csv(TRADES_CSV, parse_dates=["timestamp"])
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
        df["price"]    = pd.to_numeric(df["price"],    errors="coerce")
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as exc:
        logger.warning("trades.csv load failed: %s", exc)
        return pd.DataFrame(
            columns=["timestamp", "ticker", "action", "quantity", "price", "order_id"]
        )


@st.cache_data(ttl=60)
def _list_sessions() -> list:
    """Return [(label, Path), ...] sorted newest-first."""
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("session_*.json"), reverse=True)
    result = []
    for f in files:
        try:
            ts    = datetime.strptime(f.stem[8:], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            label = ts.astimezone(_SGT).strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            label = f.stem
        result.append((label, f))
    return result


def _read_session(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


@st.cache_data(ttl=60)
def _latest_decisions() -> list:
    sessions = _list_sessions()
    if not sessions:
        return []
    return _read_session(sessions[0][1]).get("results", [])


def _kill_switch_active() -> bool:
    return KILL_SWITCH.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Trade analytics
# ─────────────────────────────────────────────────────────────────────────────

def _match_pnl(df: pd.DataFrame) -> pd.DataFrame:
    """FIFO-match BUY/SELL pairs per ticker and return closed-trade P&L rows."""
    empty = pd.DataFrame(
        columns=["timestamp", "ticker", "pnl", "entry_price", "exit_price", "quantity"]
    )
    if df.empty:
        return empty

    trades = []
    for ticker, grp in df.groupby("ticker"):
        grp  = grp.sort_values("timestamp")
        buys: list = []
        for _, row in grp.iterrows():
            act = str(row.get("action", "")).upper()
            if act == "BUY":
                buys.append(row)
            elif act == "SELL" and buys:
                buy = buys.pop(0)
                qty = min(float(buy["quantity"]), float(row["quantity"]))
                trades.append({
                    "timestamp":   row["timestamp"],
                    "ticker":      ticker,
                    "pnl":         round((float(row["price"]) - float(buy["price"])) * qty, 2),
                    "entry_price": float(buy["price"]),
                    "exit_price":  float(row["price"]),
                    "quantity":    qty,
                })

    if not trades:
        return empty

    out = pd.DataFrame(trades).sort_values("timestamp").reset_index(drop=True)
    out["cumulative_pnl"] = out["pnl"].cumsum()
    return out


def _metrics(pnl_df: pd.DataFrame) -> dict:
    if pnl_df.empty or "pnl" not in pnl_df.columns:
        return {}

    pnl   = pnl_df["pnl"]
    wins  = pnl[pnl > 0]
    loses = pnl[pnl < 0]

    win_rate      = len(wins) / len(pnl) * 100 if len(pnl) else 0
    profit_factor = (wins.sum() / abs(loses.sum())) if (len(loses) and loses.sum() != 0) else float("inf")
    sharpe        = (pnl.mean() / pnl.std() * np.sqrt(252)) if (len(pnl) > 1 and pnl.std() > 0) else 0.0

    best_idx  = pnl.idxmax() if len(pnl) else None
    worst_idx = pnl.idxmin() if len(pnl) else None

    return {
        "total_pnl":     round(pnl.sum(), 2),
        "total_trades":  len(pnl),
        "win_rate":      round(win_rate, 1),
        "avg_win":       round(wins.mean(), 2)  if len(wins)  else 0,
        "avg_loss":      round(loses.mean(), 2) if len(loses) else 0,
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "∞",
        "sharpe":        round(sharpe, 2),
        "best_trade":    round(pnl.max(), 2) if len(pnl) else 0,
        "worst_trade":   round(pnl.min(), 2) if len(pnl) else 0,
        "best_row":      pnl_df.loc[best_idx]  if best_idx  is not None else None,
        "worst_row":     pnl_df.loc[worst_idx] if worst_idx is not None else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Page 1 — Live Overview
# ─────────────────────────────────────────────────────────────────────────────

def page_live_overview():
    st.title("📊 Live Overview")

    _, col_toggle = st.columns([5, 1])
    with col_toggle:
        auto_refresh = st.toggle("Auto-refresh", value=False, key="auto_refresh_p1")

    if auto_refresh:
        st.components.v1.html(
            '<script>setTimeout(function(){window.location.reload()}, 60000);</script>',
            height=0,
        )
        st.caption(f"Refreshing every 60s · loaded {datetime.now(_SGT).strftime('%H:%M:%S SGT')}")

    # ── Account Summary ────────────────────────────────────────────────────
    st.subheader("Account Summary")
    account   = _get_account()
    positions = _get_positions()

    unrealised_pnl = sum(p.get("unrealised_pnl", 0) for p in positions)

    if account:
        equity    = account.get("equity", 0.0)
        daily_pnl = account.get("daily_pnl", 0.0)
        daily_pct = (daily_pnl / equity * 100) if equity else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Equity",   f"${equity:,.2f}")
        c2.metric("Cash",           f"${account.get('cash', 0):,.2f}")
        c3.metric("Daily P&L",
                  f"${daily_pnl:+,.2f}",
                  delta=f"{daily_pct:+.2f}%",
                  delta_color="normal")
        c4.metric("Unrealised P&L", f"${unrealised_pnl:+,.2f}")
    else:
        st.info(
            "Alpaca not configured — set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` "
            "in `.env` to see live account data."
        )
        c1, c2, c3, c4 = st.columns(4)
        for col, lbl in zip(
            [c1, c2, c3, c4],
            ["Total Equity", "Cash", "Daily P&L", "Unrealised P&L"],
        ):
            col.metric(lbl, "—")

    st.divider()

    # ── Bot Status ─────────────────────────────────────────────────────────
    st.subheader("Bot Status")
    c1, c2, c3, c4 = st.columns(4)

    ks_active = _kill_switch_active()
    with c1:
        st.markdown("**Kill Switch**")
        if ks_active:
            st.error("🔴 ACTIVE — trading halted")
            if st.button("Deactivate", key="ks_off_p1", type="primary"):
                KILL_SWITCH.unlink(missing_ok=True)
                st.rerun()
        else:
            st.success("🟢 INACTIVE")
            if st.button("Activate", key="ks_on_p1", type="secondary"):
                KILL_SWITCH.touch()
                st.rerun()

    sessions = _list_sessions()
    with c2:
        st.markdown("**Last Run**")
        st.caption(sessions[0][0] if sessions else "No sessions yet")

    with c3:
        next_run, next_label = _next_scheduled_run()
        st.markdown("**Next Scheduled Run**")
        st.caption(f"{next_run.strftime('%Y-%m-%d %H:%M SGT')} — {next_label}")

    with c4:
        mode = os.getenv("TRADING_MODE", "paper").upper()
        st.markdown("**Trading Mode**")
        st.caption(f"{'🔴' if mode == 'LIVE' else '📋'} {mode}")

    st.divider()

    # ── Watchlist Signals ──────────────────────────────────────────────────
    st.subheader("Watchlist — Latest Signals")

    decisions = _latest_decisions()
    watchlist = [
        t.strip().upper()
        for t in os.getenv("WATCHLIST", "").split(",")
        if t.strip()
    ]

    if decisions:
        rows = []
        for r in decisions:
            d   = r.get("decision", {})
            q   = (r.get("market_data", {}).get("quote") or {})
            gr  = r.get("gate_result", {})
            gate_status = (
                "APPROVED" if gr.get("approved") else gr.get("action", "BLOCKED")
            )
            price      = q.get("price")
            change_pct = q.get("change_pct")
            rows.append({
                "Ticker":        d.get("ticker", ""),
                "Price":         f"${price:,.2f}" if price is not None else "—",
                "Change %":      f"{change_pct:+.2f}%" if change_pct is not None else "—",
                "Signal":        d.get("action", "—"),
                "Confidence":    f"{d.get('confidence', 0):.1f}/10",
                "Gate":          gate_status,
                "Last Analysed": _iso_to_sgt(d.get("analysed_at", "")),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    elif watchlist:
        st.info("No sessions yet. Run: `python run_analysis.py --watchlist`")
        st.dataframe(
            pd.DataFrame({"Ticker": watchlist,
                          "Price": ["—"] * len(watchlist),
                          "Signal": ["—"] * len(watchlist)}),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.warning("WATCHLIST is empty — set `WATCHLIST=AAPL,MSFT,...` in `.env`")


# ─────────────────────────────────────────────────────────────────────────────
# Page 2 — Open Positions
# ─────────────────────────────────────────────────────────────────────────────

def page_open_positions():
    st.title("📋 Open Positions")

    if not _executor():
        broker = os.getenv("BROKER", "moomoo").lower()
        if broker == "moomoo":
            st.info(
                "MooMoo not connected — start FutuOpenD and ensure "
                "`MOOMOO_HOST` / `MOOMOO_PORT` are set in `.env`."
            )
        else:
            st.info(
                "Alpaca not configured — set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` "
                "in `.env` to see live positions."
            )
        return

    positions = _get_positions()
    trades_df = _load_trades()

    if not positions:
        st.success("No open positions.")
        return

    total_value = sum(
        (p.get("current_price") or p.get("entry_price", 0)) * p.get("quantity", 0)
        for p in positions
    )
    total_upnl = sum(p.get("unrealised_pnl", 0) for p in positions)
    c1, c2, c3 = st.columns(3)
    c1.metric("Open Positions",       len(positions))
    c2.metric("Total Market Value",   f"${total_value:,.2f}")
    c3.metric("Total Unrealised P&L", f"${total_upnl:+,.2f}")

    st.divider()

    # Pre-load SL/TP from most recent session per ticker
    sl_tp: dict = {}
    for _, path in _list_sessions():
        session = _read_session(path)
        for r in session.get("results", []):
            t = r.get("ticker", "")
            if t not in sl_tp and r.get("sizing"):
                sl_tp[t] = {
                    "sl": r["sizing"].get("stop_loss_price"),
                    "tp": r["sizing"].get("take_profit_price"),
                }

    for pos in positions:
        ticker   = pos.get("ticker", "")
        qty      = pos.get("quantity", 0)
        entry    = pos.get("entry_price", 0.0)
        current  = pos.get("current_price") or entry
        upnl     = pos.get("unrealised_pnl", 0.0)
        upnl_pct = pos.get("unrealised_pnl_pct", 0.0)
        mkt_val  = current * qty

        # Days held from trades CSV
        days_held = "—"
        if not trades_df.empty:
            buys = trades_df[
                (trades_df["ticker"] == ticker)
                & (trades_df["action"].str.upper() == "BUY")
            ].sort_values("timestamp")
            if not buys.empty:
                buy_ts = buys.iloc[0]["timestamp"]
                if pd.notna(buy_ts):
                    delta     = datetime.now(timezone.utc) - buy_ts.replace(tzinfo=timezone.utc)
                    days_held = f"{delta.days}d"

        pnl_class = "pos-pnl" if upnl >= 0 else "neg-pnl"
        sl_info   = sl_tp.get(ticker, {})
        sl_str    = f"${sl_info['sl']:,.2f}" if sl_info.get("sl") else "—"
        tp_str    = f"${sl_info['tp']:,.2f}" if sl_info.get("tp") else "—"

        with st.container(border=True):
            row1, row2 = st.columns([8, 2])
            with row1:
                c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
                c1.markdown(f"**{ticker}**")
                c2.markdown(f"Entry: **${entry:,.2f}**")
                c3.markdown(f"Current: **${current:,.2f}**")
                c4.markdown(f"Qty: **{qty}**")
                c5.markdown(f"Value: **${mkt_val:,.0f}**")
                c6.markdown(
                    f"<span class='{pnl_class}'>${upnl:+,.2f} ({upnl_pct:+.1f}%)</span>",
                    unsafe_allow_html=True,
                )
                c7.markdown(f"SL {sl_str} · TP {tp_str} · {days_held}")
            with row2:
                if st.button(
                    f"Close {ticker}",
                    key=f"close_{ticker}",
                    type="secondary",
                    use_container_width=True,
                ):
                    ex = _executor()
                    with st.spinner(f"Closing {ticker}…"):
                        try:
                            if hasattr(ex, "close_position"):
                                order = ex.close_position(ticker)
                                oid = order.get("id") if order else None
                            else:
                                order = ex.place_order(ticker, "sell", int(qty))
                                oid = order.get("order_id") if order else None
                        except Exception as exc:
                            order = None
                            logger.warning("Close %s failed: %s", ticker, exc)
                    if order:
                        st.success(f"Close order placed — id: {oid}")
                        st.cache_resource.clear()
                        st.rerun()
                    else:
                        st.error(f"Failed to close {ticker}")


# ─────────────────────────────────────────────────────────────────────────────
# Page 3 — Trade History
# ─────────────────────────────────────────────────────────────────────────────

def page_trade_history():
    st.title("📜 Trade History")

    df     = _load_trades()
    pnl_df = _match_pnl(df)
    m      = _metrics(pnl_df)

    if df.empty:
        st.info(
            "No trades recorded yet. Trades are written to `logs/trades.csv` "
            "when orders are placed with `--execute`."
        )
        return

    # ── Metrics row ────────────────────────────────────────────────────────
    st.subheader("Performance Metrics")
    c1, c2, c3, c4, c5, c6 = st.columns(6)

    if m:
        c1.metric("Total P&L",     f"${m.get('total_pnl', 0):+,.2f}")
        c2.metric("Win Rate",      f"{m.get('win_rate', 0):.1f}%")
        c3.metric("Avg Win",       f"${m.get('avg_win', 0):,.2f}")
        c4.metric("Avg Loss",      f"${m.get('avg_loss', 0):,.2f}")
        c5.metric("Profit Factor", str(m.get("profit_factor", 0)))
        c6.metric("Sharpe Ratio",  f"{m.get('sharpe', 0):.2f}")

        ba, bb = st.columns(2)
        best  = m.get("best_row")
        worst = m.get("worst_row")
        if best is not None:
            ba.success(f"Best trade: **{best.get('ticker', '')}** +${m.get('best_trade', 0):,.2f}")
        if worst is not None:
            bb.error(f"Worst trade: **{worst.get('ticker', '')}** ${m.get('worst_trade', 0):,.2f}")
    else:
        for col in [c1, c2, c3, c4, c5, c6]:
            col.metric("—", "—")
        st.info(
            f"{len(df)} raw trade(s) logged. "
            "Metrics require matched BUY/SELL pairs."
        )

    st.divider()

    # ── Cumulative P&L chart ───────────────────────────────────────────────
    if not pnl_df.empty:
        st.subheader("Cumulative P&L")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=pnl_df["timestamp"],
            y=pnl_df["cumulative_pnl"],
            mode="lines+markers",
            line=dict(color="#00c46a", width=2),
            marker=dict(size=5),
            fill="tozeroy",
            fillcolor="rgba(0,196,106,0.08)",
        ))
        fig.update_layout(
            template="plotly_dark",
            height=300,
            margin=dict(l=0, r=0, t=10, b=0),
            yaxis_title="P&L ($)",
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
        st.divider()

    # ── Raw trade log ──────────────────────────────────────────────────────
    st.subheader(f"Trade Log  ({len(df)} entries)")
    display = df.copy().sort_values("timestamp", ascending=False)
    ts_col = display["timestamp"]
    if ts_col.dt.tz is None:
        ts_col = ts_col.dt.tz_localize("UTC")
    display["timestamp"] = ts_col.dt.tz_convert("Asia/Singapore").dt.strftime("%Y-%m-%d %H:%M:%S SGT")
    display["price"]     = display["price"].map("${:,.2f}".format)
    st.dataframe(display, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Page 4 — Bot Decisions
# ─────────────────────────────────────────────────────────────────────────────

def page_bot_decisions():
    st.title("🤖 Bot Decisions — Agent Reasoning Viewer")

    sessions = _list_sessions()
    if not sessions:
        st.info("No session logs found in `logs/`. Run an analysis to populate this page.")
        return

    labels = [lbl for lbl, _ in sessions]
    sel    = st.selectbox("Session", labels, index=0)
    idx    = labels.index(sel)
    data   = _read_session(sessions[idx][1])
    results = data.get("results", [])

    if not results:
        st.warning("No results in this session.")
        return

    st.caption(
        f"Session `{data.get('session_id', '')}` · "
        f"{data.get('ticker_count', len(results))} ticker(s) · "
        f"generated {_iso_to_sgt(data.get('generated_at', ''))}"
    )

    for r in results:
        decision = r.get("decision", {})
        gate     = r.get("gate_result", {})
        sizing   = r.get("sizing")
        order    = r.get("order")
        mkt      = r.get("market_data", {})
        ticker   = r.get("ticker", decision.get("ticker", ""))
        action   = decision.get("action", "HOLD")
        conf     = decision.get("confidence", 0.0)
        elapsed  = r.get("elapsed_seconds", 0.0)
        error    = r.get("error")
        approved = gate.get("approved", False)

        if action == "BUY":
            act_tag = '<span class="tag tag-buy">BUY</span>'
        elif action == "SELL":
            act_tag = '<span class="tag tag-sell">SELL</span>'
        else:
            act_tag = '<span class="tag tag-hold">HOLD</span>'

        if approved:
            gate_tag = '<span class="tag tag-approved">APPROVED</span>'
        elif gate.get("action") == "HOLD":
            gate_tag = '<span class="tag tag-hold">HOLD</span>'
        else:
            gate_tag = '<span class="tag tag-blocked">BLOCKED</span>'

        st.markdown(
            f"### {ticker} &nbsp; {act_tag} &nbsp; {gate_tag} &nbsp;"
            f"<span style='color:#888;font-size:0.85em'>"
            f"conf {conf:.1f}/10 · {elapsed:.0f}s</span>",
            unsafe_allow_html=True,
        )

        if error:
            st.error(f"Pipeline error: {error}")

        q = (mkt.get("quote") or {})
        if q.get("price"):
            chg = q.get("change_pct") or 0.0
            st.caption(f"Price at analysis: ${q['price']:,.2f}  ·  Change: {chg:+.2f}%")

        col_l, col_r = st.columns(2)

        with col_l:
            with st.expander("⚡ Risk Gate"):
                st.markdown(f"**{'✅ APPROVED' if approved else '❌ BLOCKED / HOLD'}**")
                st.write(gate.get("reason", "—"))

            with st.expander("📝 Fund Manager Reasoning"):
                st.write(decision.get("reasoning", "—"))

            with st.expander("🐂 Bull Case"):
                st.write(decision.get("bull_case", "—"))

            with st.expander("🐻 Bear Case"):
                st.write(decision.get("bear_case", "—"))

        with col_r:
            flags = decision.get("risk_flags", [])
            if flags:
                with st.expander(f"⚠️ Risk Flags ({len(flags)})"):
                    for flag in flags:
                        st.markdown(f"• {flag}")

            if sizing:
                with st.expander("📐 Position Sizing"):
                    sc1, sc2 = st.columns(2)
                    sc1.metric("Qty",         f"{sizing.get('quantity', 0)} sh")
                    sc2.metric("Entry",       f"${sizing.get('entry_price', 0):,.2f}")
                    sc1.metric("Stop Loss",   f"${sizing.get('stop_loss_price', 0):,.2f}")
                    sc2.metric("Take Profit", f"${sizing.get('take_profit_price', 0):,.2f}")
                    sc1.metric("Position $",  f"${sizing.get('position_value', 0):,.0f}")
                    sc2.metric("Position %",  f"{sizing.get('position_pct', 0) * 100:.1f}%")
                    st.metric("Risk Amount",  f"${sizing.get('risk_amount', 0):,.2f}")

            if order:
                with st.expander("📋 Order"):
                    st.json(order)

            tech = mkt.get("technicals", {})
            if any(tech.get(k) is not None for k in ("rsi_14", "macd", "bb_upper")):
                with st.expander("📊 Technicals"):
                    tc1, tc2 = st.columns(2)
                    rsi  = tech.get("rsi_14")
                    macd = tech.get("macd")
                    bbu  = tech.get("bb_upper")
                    bbl  = tech.get("bb_lower")
                    tc1.metric("RSI-14",   f"{rsi:.2f}"   if rsi  is not None else "—")
                    tc2.metric("MACD",     f"{macd:.4f}"  if macd is not None else "—")
                    tc1.metric("BB Upper", f"${bbu:,.2f}" if bbu  is not None else "—")
                    tc2.metric("BB Lower", f"${bbl:,.2f}" if bbl  is not None else "—")

        news = mkt.get("news", [])
        if news:
            with st.expander(f"📰 News ({min(len(news), 5)} headlines)"):
                for article in news[:5]:
                    st.markdown(
                        f"• {article.get('headline', '')} "
                        f"_({article.get('source', '')})_"
                    )

        st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# Page 5 — Performance Analytics
# ─────────────────────────────────────────────────────────────────────────────

def page_performance():
    st.title("📈 Performance Analytics")

    df = _load_trades()
    if df.empty:
        st.info("No trade data yet. Performance analytics appear once trades are logged.")
        return

    pnl_df = _match_pnl(df)
    if pnl_df.empty:
        st.info(
            f"No closed trades yet ({len(df)} raw entries logged). "
            "Metrics require matched BUY/SELL pairs."
        )
        return

    initial_equity = float(os.getenv("PAPER_EQUITY", "10000"))
    pnl_df = pnl_df.copy()
    pnl_df["equity"] = initial_equity + pnl_df["cumulative_pnl"]

    # ── Equity curve ───────────────────────────────────────────────────────
    st.subheader("Equity Curve")
    fig_eq = go.Figure()
    fig_eq.add_trace(go.Scatter(
        x=pnl_df["timestamp"],
        y=pnl_df["equity"],
        name="Portfolio",
        mode="lines",
        line=dict(color="#00c46a", width=2),
    ))

    # Optional SPY benchmark via yfinance
    try:
        import yfinance as yf  # type: ignore  # noqa: F401
        start = pnl_df["timestamp"].min().strftime("%Y-%m-%d")
        end   = pnl_df["timestamp"].max().strftime("%Y-%m-%d")
        if start != end:
            spy = yf.download("SPY", start=start, end=end, progress=False)
            if not spy.empty:
                spy_close = spy["Close"].squeeze()
                spy_norm  = (spy_close / spy_close.iloc[0]) * initial_equity
                fig_eq.add_trace(go.Scatter(
                    x=spy_norm.index,
                    y=spy_norm.values,
                    name="SPY (benchmark)",
                    mode="lines",
                    line=dict(color="#888", width=1, dash="dash"),
                ))
    except Exception:
        pass

    fig_eq.update_layout(
        template="plotly_dark",
        height=380,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Portfolio Value ($)",
        legend=dict(x=0.01, y=0.99),
    )
    st.plotly_chart(fig_eq, use_container_width=True)

    # ── Drawdown ───────────────────────────────────────────────────────────
    st.subheader("Drawdown")
    peak     = pnl_df["equity"].cummax()
    drawdown = (pnl_df["equity"] - peak) / peak * 100

    fig_dd = go.Figure()
    fig_dd.add_trace(go.Scatter(
        x=pnl_df["timestamp"],
        y=drawdown,
        mode="lines",
        fill="tozeroy",
        fillcolor="rgba(255,75,75,0.2)",
        line=dict(color="#ff4b4b", width=1),
    ))
    fig_dd.update_layout(
        template="plotly_dark",
        height=220,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="Drawdown %",
        showlegend=False,
    )
    st.plotly_chart(fig_dd, use_container_width=True)

    # ── Monthly returns heatmap ────────────────────────────────────────────
    st.subheader("Monthly Returns")
    if len(pnl_df) >= 2:
        try:
            monthly = (
                pnl_df.set_index("timestamp")["pnl"]
                .resample("ME")
                .sum()
                .reset_index()
            )
            monthly.columns = ["date", "pnl"]
            monthly["year"]  = monthly["date"].dt.year.astype(str)
            monthly["month"] = monthly["date"].dt.strftime("%b")
            month_order = ["Jan","Feb","Mar","Apr","May","Jun",
                           "Jul","Aug","Sep","Oct","Nov","Dec"]
            pivot = monthly.pivot_table(
                index="year", columns="month", values="pnl", aggfunc="sum"
            ).reindex(columns=[m for m in month_order if m in monthly["month"].values])

            if not pivot.empty:
                fig_heat = px.imshow(
                    pivot,
                    color_continuous_scale="RdYlGn",
                    color_continuous_midpoint=0,
                    aspect="auto",
                    labels={"color": "P&L ($)"},
                    template="plotly_dark",
                    text_auto=".0f",
                )
                fig_heat.update_layout(
                    height=max(160, 80 * len(pivot)),
                    margin=dict(l=0, r=0, t=10, b=0),
                )
                st.plotly_chart(fig_heat, use_container_width=True)
        except Exception as exc:
            st.caption(f"Monthly heatmap unavailable: {exc}")
    else:
        st.info("More closed trades needed to build the monthly heatmap.")

    col_left, col_right = st.columns(2)

    # ── P&L by ticker ──────────────────────────────────────────────────────
    with col_left:
        st.subheader("P&L by Ticker")
        ticker_pnl = (
            pnl_df.groupby("ticker")["pnl"].sum().sort_values().reset_index()
        )
        ticker_pnl.columns = ["Ticker", "P&L"]
        fig_bar = px.bar(
            ticker_pnl,
            x="Ticker",
            y="P&L",
            color="P&L",
            color_continuous_scale="RdYlGn",
            color_continuous_midpoint=0,
            template="plotly_dark",
        )
        fig_bar.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=10, b=0),
            coloraxis_showscale=False,
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    # ── Confidence vs outcome ──────────────────────────────────────────────
    with col_right:
        st.subheader("Confidence vs Outcome")
        conf_rows = []
        for _, path in _list_sessions():
            for r in _read_session(path).get("results", []):
                d      = r.get("decision", {})
                t_name = d.get("ticker")
                conf   = d.get("confidence", 0.0)
                ts_str = d.get("analysed_at", "")
                if not t_name or not ts_str:
                    continue
                try:
                    ts     = pd.Timestamp(ts_str)
                    nearby = pnl_df[
                        (pnl_df["ticker"] == t_name)
                        & (abs((pnl_df["timestamp"] - ts).dt.total_seconds()) < 7200)
                    ]
                    if not nearby.empty:
                        conf_rows.append({
                            "Ticker":     t_name,
                            "Confidence": conf,
                            "P&L":        nearby.iloc[0]["pnl"],
                        })
                except Exception:
                    pass

        if conf_rows:
            cdf    = pd.DataFrame(conf_rows)
            fig_sc = px.scatter(
                cdf,
                x="Confidence",
                y="P&L",
                color="P&L",
                color_continuous_scale="RdYlGn",
                color_continuous_midpoint=0,
                hover_data=["Ticker"],
                labels={"Confidence": "Agent Confidence (1–10)", "P&L": "Trade P&L ($)"},
                template="plotly_dark",
            )
            fig_sc.add_hline(y=0, line_dash="dash", line_color="gray")
            fig_sc.update_layout(
                height=300,
                margin=dict(l=0, r=0, t=10, b=0),
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_sc, use_container_width=True)
        else:
            st.info(
                "Populates once analysis decisions result in closed trades. "
                "Requires matched BUY/SELL pairs and session logs."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Page 6 — Settings & Controls
# ─────────────────────────────────────────────────────────────────────────────

def page_settings():
    st.title("⚙️ Settings & Controls")

    # ── Kill switch ────────────────────────────────────────────────────────
    st.subheader("Kill Switch")
    ks_active = _kill_switch_active()
    c1, c2 = st.columns([4, 1])
    with c1:
        if ks_active:
            st.error(f"🔴 **ACTIVE** — All trading halted.  Lock: `{KILL_SWITCH}`")
        else:
            st.success("🟢 **INACTIVE** — Trading enabled")
    with c2:
        if ks_active:
            if st.button("Deactivate", type="primary", use_container_width=True, key="ks_d"):
                KILL_SWITCH.unlink(missing_ok=True)
                st.success("Deactivated")
                st.rerun()
        else:
            if st.button("Activate", type="secondary", use_container_width=True, key="ks_a"):
                KILL_SWITCH.touch()
                st.warning("Activated")
                st.rerun()

    st.divider()

    # ── Watchlist editor ───────────────────────────────────────────────────
    st.subheader("Watchlist")
    with st.form("watchlist_form"):
        new_wl = st.text_input(
            "Tickers (comma-separated)",
            value=os.getenv("WATCHLIST", ""),
            placeholder="AAPL,MSFT,GOOGL,NVDA",
        )
        if st.form_submit_button("Save Watchlist"):
            tickers = [t.strip().upper() for t in new_wl.split(",") if t.strip()]
            clean   = ",".join(tickers)
            if ENV_FILE.exists():
                try:
                    set_key(str(ENV_FILE), "WATCHLIST", clean)
                    os.environ["WATCHLIST"] = clean
                    _list_sessions.clear()
                    st.success(f"Saved: `{clean}`")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
            else:
                st.error(".env not found")

    st.divider()

    # ── Risk parameters ────────────────────────────────────────────────────
    st.subheader("Risk Parameters")
    with st.form("risk_form"):
        c1, c2 = st.columns(2)
        with c1:
            min_conf = st.number_input(
                "Min Confidence Score (1–10)",
                min_value=1.0, max_value=10.0,
                value=float(os.getenv("MIN_CONFIDENCE_SCORE", "7.0")),
                step=0.5,
            )
            max_pos_pct = st.number_input(
                "Max Position Size % (e.g. 0.05 = 5%)",
                min_value=0.01, max_value=1.0,
                value=float(os.getenv("MAX_POSITION_SIZE_PCT", "0.05")),
                step=0.01, format="%.2f",
            )
        with c2:
            max_loss_pct = st.number_input(
                "Max Daily Loss % (e.g. 0.02 = 2%)",
                min_value=0.001, max_value=0.5,
                value=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.02")),
                step=0.005, format="%.3f",
            )
            max_trades = st.number_input(
                "Max Trades Per Day",
                min_value=1, max_value=50,
                value=int(os.getenv("MAX_TRADES_PER_DAY", "5")),
                step=1,
            )

        if st.form_submit_button("Save Risk Parameters"):
            if ENV_FILE.exists():
                try:
                    set_key(str(ENV_FILE), "MIN_CONFIDENCE_SCORE", str(min_conf))
                    set_key(str(ENV_FILE), "MAX_POSITION_SIZE_PCT", str(max_pos_pct))
                    set_key(str(ENV_FILE), "MAX_DAILY_LOSS_PCT",    str(max_loss_pct))
                    set_key(str(ENV_FILE), "MAX_TRADES_PER_DAY",    str(int(max_trades)))
                    os.environ.update({
                        "MIN_CONFIDENCE_SCORE": str(min_conf),
                        "MAX_POSITION_SIZE_PCT": str(max_pos_pct),
                        "MAX_DAILY_LOSS_PCT":    str(max_loss_pct),
                        "MAX_TRADES_PER_DAY":    str(int(max_trades)),
                    })
                    st.success("Risk parameters saved to .env")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
            else:
                st.error(".env not found")

    st.divider()

    # ── Manual trigger ─────────────────────────────────────────────────────
    st.subheader("Manual Analysis Trigger")

    ks_active = _kill_switch_active()
    if ks_active:
        st.warning("Kill switch is active — deactivate it before running analysis.")

    tc1, tc2, tc3 = st.columns([3, 2, 2])
    with tc1:
        run_wl = st.button(
            "▶ Run Watchlist Analysis",
            type="primary",
            use_container_width=True,
            disabled=ks_active,
        )
    with tc2:
        single    = st.text_input("Single ticker", placeholder="AAPL", key="manual_ticker")
        run_single = st.button(
            "▶ Run Ticker",
            use_container_width=True,
            disabled=ks_active,
        )
    with tc3:
        st.markdown("")
        execute_flag = st.toggle(
            "--execute (place orders)",
            value=False,
            help="Adds --execute so approved trades are sent to Alpaca",
        )

    if run_wl or (run_single and single.strip()):
        if run_wl:
            cmd = [sys.executable, str(_ROOT / "run_analysis.py"), "--watchlist"]
        else:
            cmd = [sys.executable, str(_ROOT / "run_analysis.py"),
                   "--ticker", single.strip().upper()]
        if execute_flag:
            cmd.append("--execute")

        st.code(" ".join(cmd), language="bash")
        with st.spinner("Running analysis — 2–3 min per ticker…"):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(_ROOT),
                    timeout=600,
                )
                if proc.returncode == 0:
                    st.success("Analysis complete — refresh other pages to see results.")
                    st.cache_data.clear()
                else:
                    st.error(f"Exit code {proc.returncode}")
                if proc.stderr:
                    with st.expander("Stderr"):
                        st.code(proc.stderr[-3000:], language="bash")
                if proc.stdout:
                    with st.expander("Stdout"):
                        st.code(proc.stdout[-3000:], language="bash")
            except subprocess.TimeoutExpired:
                st.error("Timed out after 10 minutes")
            except Exception as exc:
                st.error(f"Launch failed: {exc}")

    st.divider()

    # ── API status panel ───────────────────────────────────────────────────
    st.subheader("API Status")
    _broker = os.getenv("BROKER", "moomoo").lower()
    broker_entry = ("MooMoo", "MOOMOO_HOST") if _broker == "moomoo" else ("Alpaca", "ALPACA_API_KEY")
    apis = [
        ("Anthropic",     "ANTHROPIC_API_KEY"),
        ("Alpha Vantage", "ALPHA_VANTAGE_API_KEY"),
        ("Finnhub",       "FINNHUB_API_KEY"),
        ("FMP",           "FMP_API_KEY"),
        broker_entry,
        ("Telegram",      "TELEGRAM_BOT_TOKEN"),
    ]
    cols = st.columns(len(apis))
    for col, (name, key) in zip(cols, apis):
        val        = os.getenv(key, "")
        configured = bool(val) and val not in ("your-key-here", "")
        masked     = f"…{val[-4:]}" if configured and len(val) >= 4 else "not set"
        col.metric(name, "✅" if configured else "⚠️", masked)

    st.markdown("")
    btn_label = "Test MooMoo Connection" if _broker == "moomoo" else "Test Alpaca Connection"
    if st.button(btn_label):
        acct = _get_account()
        if acct:
            mode = os.getenv("TRADING_MODE", "paper").upper()
            st.success(f"Connected ✓  equity=${acct['equity']:,.2f}  mode={mode}")
        else:
            if _broker == "moomoo":
                host = os.getenv("MOOMOO_HOST", "127.0.0.1")
                port = os.getenv("MOOMOO_PORT", "11111")
                st.error(
                    f"MooMoo unreachable — check FutuOpenD is running at {host}:{port} "
                    "and trading is unlocked."
                )
            else:
                st.error("Alpaca not configured — check ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")


# ─────────────────────────────────────────────────────────────────────────────
# Navigation / entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    st.sidebar.title("📈 AI Trading Bot")
    st.sidebar.markdown("---")

    pages = {
        "📊 Live Overview":       page_live_overview,
        "📋 Open Positions":      page_open_positions,
        "📜 Trade History":       page_trade_history,
        "🤖 Bot Decisions":       page_bot_decisions,
        "📈 Performance":         page_performance,
        "⚙️ Settings & Controls": page_settings,
    }

    selection = st.sidebar.radio(
        "Navigation",
        list(pages.keys()),
        label_visibility="collapsed",
    )

    st.sidebar.markdown("---")
    ks   = _kill_switch_active()
    mode = os.getenv("TRADING_MODE", "paper").upper()
    sessions = _list_sessions()
    st.sidebar.markdown(
        f"**Kill Switch:** {'🔴 ON' if ks else '🟢 OFF'}  \n"
        f"**Mode:** {'🔴 LIVE' if mode == 'LIVE' else '📋 PAPER'}"
    )
    if sessions:
        st.sidebar.caption(f"Last run: {sessions[0][0]}")
    else:
        st.sidebar.caption("No sessions yet")

    pages[selection]()


if __name__ == "__main__":
    main()
