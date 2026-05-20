#!/usr/bin/env python3
"""
run_analysis.py — Main analysis runner for the AI trading bot.

Usage:
    python run_analysis.py --watchlist          # Run every ticker in WATCHLIST env var
    python run_analysis.py --ticker AAPL        # Run a single ticker
    python run_analysis.py --ticker AAPL MSFT   # Run specific tickers

The runner:
  1. Fetches market data via DataFetcher
  2. Runs the 7-agent pipeline via TradingAgentsWrapper
  3. Runs RiskGate + PositionSizer on each decision
  4. If APPROVED: checks market hours, places order via AlpacaExecutor,
     sends trade_placed Telegram alert
  5. If BLOCKED: sends trade_blocked Telegram alert
  6. Logs each decision + raw data snapshot via monitoring/logger.py
  7. Prints a clean per-ticker summary as it goes
  8. Prints a ranked confidence table at the end
  9. Saves the full session (decisions + gate results + sizing + orders) to
     logs/session_YYYYMMDD_HHMMSS.json
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup — structured output to stderr, clean summaries to stdout
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("run_analysis")

# ---------------------------------------------------------------------------
# ANSI colours (stripped automatically when stdout is not a TTY)
# ---------------------------------------------------------------------------

def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_USE_COLOUR = _is_tty()

_C = {
    "BUY":      "\033[92m",   # green
    "SELL":     "\033[91m",   # red
    "HOLD":     "\033[93m",   # yellow
    "APPROVED": "\033[92m",   # green
    "BLOCKED":  "\033[91m",   # red
    "BOLD":     "\033[1m",
    "DIM":      "\033[2m",
    "RESET":    "\033[0m",
}

def _col(text: str, key: str) -> str:
    if not _USE_COLOUR:
        return text
    return f"{_C.get(key, '')}{text}{_C['RESET']}"


# ---------------------------------------------------------------------------
# Mock portfolio state — replaced by live Alpaca account state in Phase 5
# ---------------------------------------------------------------------------

_MOCK_PORTFOLIO: dict = {
    "equity":          10_000.0,
    "portfolio_value": 10_000.0,
    "trades_today":    0,
    "daily_pnl":       0.0,
    "open_positions":  [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_watchlist() -> list:
    raw = os.getenv("WATCHLIST", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tickers:
        logger.error("WATCHLIST is empty or not set in .env")
        sys.exit(1)
    return tickers


def _print_banner(tickers: list) -> None:
    width = 60
    print()
    print("=" * width)
    print(_col("  AI Trading Bot — Analysis Session", "BOLD").center(width + 10))
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Tickers: {', '.join(tickers)}")
    print("=" * width)
    print()


def _print_ticker_result(
    decision: dict,
    gate_result: dict,
    sizing: Optional[dict],
    elapsed: float,
    order: Optional[dict] = None,
    market_open: Optional[bool] = None,
) -> None:
    ticker     = decision["ticker"]
    action     = decision["action"]
    confidence = decision["confidence"]
    reasoning  = decision["reasoning"]
    risk_flags = decision["risk_flags"]

    action_col = _col(f"{action:4}", action)
    conf_col   = _col(f"{confidence:4.1f}/10", "BOLD")

    print(f"  {'─' * 56}")
    print(f"  {_col(ticker, 'BOLD'):10}  {action_col}  conf {conf_col}  ({elapsed:.1f}s)")
    print(f"  {_col('Reasoning:', 'DIM')} {reasoning[:100]}{'…' if len(reasoning) > 100 else ''}")
    if risk_flags:
        print(f"  {_col('Risks:    ', 'DIM')} {risk_flags[0][:90]}{'…' if len(risk_flags[0]) > 90 else ''}")
        for flag in risk_flags[1:]:
            print(f"             {flag[:90]}{'…' if len(flag) > 90 else ''}")

    print()

    # ── Risk gate / sizing / execution output ─────────────────────────
    if gate_result["approved"] and sizing:
        qty   = sizing["quantity"]
        price = sizing["entry_price"]
        val   = sizing["position_value"]
        pct   = sizing["position_pct"] * 100
        sl    = sizing["stop_loss_price"]
        tp    = sizing["take_profit_price"]
        risk  = sizing["risk_amount"]
        print(
            f"  {_col('APPROVED', 'APPROVED')}: "
            f"{action} {qty} shares of {ticker} @ ${price:,.2f}"
        )
        print(f"  Position:   ${val:,.0f} ({pct:.1f}% of portfolio)")
        print(f"  Stop loss:  ${sl:,.2f} | Take profit: ${tp:,.2f}")
        print(f"  Risk:       ${risk:,.2f}")
        # Execution status
        if order:
            print(
                f"  {_col('Order placed', 'APPROVED')}:  "
                f"id={order.get('id')}  status={order.get('status')}"
            )
        elif market_open is False:
            print(f"  {_col('Market closed', 'DIM')} — execution skipped")
        elif market_open is None:
            print(f"  {_col('Broker not configured', 'DIM')} — execution skipped")

    elif gate_result["approved"] and not sizing:
        print(
            f"  {_col('APPROVED', 'APPROVED')}: {action} {ticker} "
            f"(no price data — manual sizing required)"
        )

    elif gate_result["action"] == "HOLD":
        print(f"  {_col('HOLD', 'HOLD')}: {gate_result['reason']}")

    else:
        print(f"  {_col('BLOCKED', 'BLOCKED')}: {gate_result['reason']}")

    print()


def _trade_plan_str(gate_result: dict, sizing: Optional[dict]) -> str:
    """Compact one-liner for the summary table TRADE PLAN column."""
    if gate_result.get("approved") and sizing:
        return (
            f"{sizing['quantity']} sh @ ${sizing['entry_price']:,.2f} | "
            f"SL ${sizing['stop_loss_price']:,.2f} → TP ${sizing['take_profit_price']:,.2f}"
        )
    reason = gate_result.get("reason", "")
    return reason[:50] + ("…" if len(reason) > 50 else "")


def _print_summary_table(results: list) -> None:
    sorted_results = sorted(
        results, key=lambda r: r["decision"]["confidence"], reverse=True
    )

    col = {"ticker": 8, "action": 6, "conf": 6, "status": 8, "plan": 52}
    total_w = sum(col.values()) + 10   # gaps between columns

    header = (
        f"  {'TICKER':<{col['ticker']}}  "
        f"{'ACTION':<{col['action']}}  "
        f"{'CONF':>{col['conf']}}  "
        f"{'STATUS':<{col['status']}}  "
        f"{'TRADE PLAN':<{col['plan']}}"
    )
    divider = "  " + "─" * total_w

    print()
    print("=" * (total_w + 2))
    print(_col("  SESSION SUMMARY — ranked by confidence", "BOLD"))
    print("=" * (total_w + 2))
    print(_col(header, "DIM"))
    print(divider)

    for r in sorted_results:
        d      = r["decision"]
        gr     = r.get("gate_result", {})
        sizing = r.get("sizing")

        ticker = d["ticker"]
        action = d["action"]
        conf   = d["confidence"]

        if gr.get("approved"):
            status = "APPROVED"
        else:
            status = gr.get("action", "BLOCKED")   # "HOLD" or "BLOCKED"

        plan = _trade_plan_str(gr, sizing)

        action_col = _col(f"{action:<{col['action']}}", action)
        status_col = _col(f"{status:<{col['status']}}", status)
        conf_str   = f"{conf:>{col['conf']}.1f}"

        print(
            f"  {ticker:<{col['ticker']}}  "
            f"{action_col}  "
            f"{conf_str}  "
            f"{status_col}  "
            f"{plan}"
        )

    print(divider)
    buy_count      = sum(1 for r in results if r["decision"]["action"] == "BUY")
    hold_count     = sum(1 for r in results if r["decision"]["action"] == "HOLD")
    sell_count     = sum(1 for r in results if r["decision"]["action"] == "SELL")
    approved_count = sum(1 for r in results if r.get("gate_result", {}).get("approved"))
    blocked_count  = len(results) - approved_count
    print(
        f"  Signals: {_col(f'{buy_count} BUY', 'BUY')}  "
        f"{_col(f'{hold_count} HOLD', 'HOLD')}  "
        f"{_col(f'{sell_count} SELL', 'SELL')}  ·  "
        f"Gate: {_col(f'{approved_count} APPROVED', 'APPROVED')}  "
        f"{_col(f'{blocked_count} BLOCKED', 'BLOCKED')}"
    )
    print()


def _save_session(results: list, session_ts: str) -> str:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    path = logs_dir / f"session_{session_ts}.json"

    session = {
        "session_id":   session_ts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticker_count": len(results),
        "results":      results,
    }

    with open(path, "w") as f:
        json.dump(session, f, indent=2, default=str)

    logger.info("Session saved → %s", path)
    return str(path)


# ---------------------------------------------------------------------------
# Helpers for error-path returns
# ---------------------------------------------------------------------------

def _error_decision(ticker: str, reasoning: str, flag: str) -> dict:
    return {
        "ticker":      ticker,
        "action":      "HOLD",
        "confidence":  1.0,
        "reasoning":   reasoning,
        "bull_case":   "",
        "bear_case":   "",
        "risk_flags":  [flag],
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


def _blocked_gate(decision: dict, reason: str) -> dict:
    return {
        "approved":          False,
        "action":            "BLOCKED",
        "reason":            reason,
        "original_decision": decision,
    }


# ---------------------------------------------------------------------------
# Core per-ticker runner
# ---------------------------------------------------------------------------

def run_ticker(ticker: str, execute: bool = False) -> dict:
    """
    Fetch data, run the full agent pipeline, then apply RiskGate + PositionSizer.

    Args:
        execute: when True, attempt order placement via AlpacaExecutor.
                 When False, analysis only — no trades placed.

    Returns:
        {
          ticker, decision, gate_result, sizing,
          market_data, elapsed_seconds, error
        }
    """
    from data.fetcher import DataFetcher
    from agents.trading_agents import TradingAgentsWrapper
    from monitoring.logger import log_decision
    from risk.risk_gate import RiskGate
    from risk.position_sizer import PositionSizer, PositionSizerError

    t0 = time.monotonic()
    logger.info("Starting analysis for %s", ticker)

    # ── 1. Fetch market data ────────────────────────────────────────────
    try:
        data = DataFetcher().fetch(ticker)
    except Exception as exc:
        logger.error("[%s] DataFetcher failed: %s", ticker, exc, exc_info=True)
        decision = _error_decision(ticker, f"Data fetch failed: {exc}", str(exc))
        return {
            "ticker":          ticker,
            "decision":        decision,
            "gate_result":     _blocked_gate(decision, f"Data fetch failed: {exc}"),
            "sizing":          None,
            "order":           None,
            "market_open":     None,
            "market_data":     {},
            "elapsed_seconds": time.monotonic() - t0,
            "error":           str(exc),
        }

    # ── 2. Run 7-agent pipeline ─────────────────────────────────────────
    try:
        decision = TradingAgentsWrapper().analyse(data)
    except Exception as exc:
        logger.error("[%s] Agent pipeline failed: %s", ticker, exc, exc_info=True)
        decision = _error_decision(ticker, f"Agent pipeline failed: {exc}", str(exc))
        return {
            "ticker":          ticker,
            "decision":        decision,
            "gate_result":     _blocked_gate(decision, f"Agent pipeline failed: {exc}"),
            "sizing":          None,
            "order":           None,
            "market_open":     None,
            "market_data":     data,
            "elapsed_seconds": time.monotonic() - t0,
            "error":           str(exc),
        }

    elapsed = time.monotonic() - t0
    log_decision(ticker, decision, data)

    # ── 3. Risk Gate ────────────────────────────────────────────────────
    gate_result = RiskGate().check(decision, _MOCK_PORTFOLIO)

    # ── 4. Position Sizer (approved BUY / SELL only) ─────────────────────
    sizing = None
    if gate_result["approved"] and decision["action"] in ("BUY", "SELL"):
        entry_price = (data.get("quote") or {}).get("price")
        if entry_price is not None:
            try:
                sizing = PositionSizer().calculate(
                    ticker=ticker,
                    action=decision["action"],
                    entry_price=float(entry_price),
                    portfolio_value=_MOCK_PORTFOLIO["portfolio_value"],
                    confidence=decision["confidence"],
                )
            except PositionSizerError as exc:
                logger.warning("[%s] PositionSizer blocked trade: %s", ticker, exc)
                gate_result = {
                    **gate_result,
                    "approved": False,
                    "action":   "BLOCKED",
                    "reason":   str(exc),
                }
        else:
            logger.warning(
                "[%s] Quote price unavailable — position sizing skipped", ticker
            )

    # ── 5. Execution + Telegram alerts ──────────────────────────────────
    from execution.alpaca import AlpacaExecutor
    from monitoring.telegram_alerts import TelegramAlerter

    alerter     = TelegramAlerter()
    order       = None
    market_open = None

    if gate_result["approved"] and sizing and execute:
        api_key = os.getenv("ALPACA_API_KEY", "")
        if api_key in ("", "your-key-here"):
            logger.info("[%s] Alpaca not configured — skipping execution", ticker)
        else:
            try:
                executor    = AlpacaExecutor()
                market_open = executor.is_market_open()
                if market_open:
                    order = executor.place_order(
                        ticker=ticker,
                        action=decision["action"],
                        quantity=sizing["quantity"],
                    )
                    if order:
                        alerter.trade_placed(
                            ticker=ticker,
                            action=decision["action"],
                            quantity=sizing["quantity"],
                            price=sizing["entry_price"],
                            order_id=order.get("id", ""),
                        )
                    else:
                        logger.error("[%s] Order placement failed", ticker)
                else:
                    logger.info("[%s] Market closed — skipping execution", ticker)
            except Exception as exc:
                logger.warning("[%s] Execution error: %s", ticker, exc, exc_info=True)

    elif gate_result["action"] == "BLOCKED":
        alerter.trade_blocked(
            ticker=ticker,
            action=decision["action"],
            confidence=decision["confidence"],
            reason=gate_result["reason"],
        )

    return {
        "ticker":          ticker,
        "decision":        decision,
        "gate_result":     gate_result,
        "sizing":          sizing,
        "order":           order,
        "market_open":     market_open,
        "market_data":     data,
        "elapsed_seconds": elapsed,
        "error":           None,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Trading Bot — run analysis on one or more tickers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_analysis.py --watchlist
  python run_analysis.py --ticker AAPL
  python run_analysis.py --ticker AAPL MSFT NVDA
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--watchlist",
        action="store_true",
        help="Run every ticker in the WATCHLIST env var",
    )
    group.add_argument(
        "--ticker",
        nargs="+",
        metavar="TICKER",
        help="One or more ticker symbols to analyse",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Place orders via AlpacaExecutor when trades are approved (default: analysis only)",
    )

    args = parser.parse_args()
    tickers = _load_watchlist() if args.watchlist else [t.upper() for t in args.ticker]

    session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _print_banner(tickers)

    results: list = []
    failed:  list = []

    for i, ticker in enumerate(tickers, start=1):
        print(f"  [{i}/{len(tickers)}] Analysing {_col(ticker, 'BOLD')}...")
        result = run_ticker(ticker, execute=args.execute)
        results.append(result)

        _print_ticker_result(
            result["decision"],
            result["gate_result"],
            result.get("sizing"),
            result["elapsed_seconds"],
            order=result.get("order"),
            market_open=result.get("market_open"),
        )

        if result["error"]:
            failed.append(ticker)

    _print_summary_table(results)

    session_path = _save_session(results, session_ts)
    print(f"  Full session saved → {_col(session_path, 'DIM')}")

    if failed:
        print(
            f"\n  {_col('Errors:', 'SELL')} "
            f"{', '.join(failed)} failed — check logs above.\n"
        )
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
