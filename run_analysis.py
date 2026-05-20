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
  3. Logs each decision + raw data snapshot via monitoring/logger.py
  4. Prints a clean per-ticker summary as it goes
  5. Prints a ranked confidence table at the end
  6. Saves the full session to logs/session_YYYYMMDD_HHMMSS.json
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
    "BUY":    "\033[92m",   # green
    "SELL":   "\033[91m",   # red
    "HOLD":   "\033[93m",   # yellow
    "BOLD":   "\033[1m",
    "DIM":    "\033[2m",
    "RESET":  "\033[0m",
}

def _col(text: str, key: str) -> str:
    if not _USE_COLOUR:
        return text
    return f"{_C.get(key, '')}{text}{_C['RESET']}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_watchlist() -> list[str]:
    raw = os.getenv("WATCHLIST", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tickers:
        logger.error("WATCHLIST is empty or not set in .env")
        sys.exit(1)
    return tickers


def _print_banner(tickers: list[str]) -> None:
    width = 60
    print()
    print("=" * width)
    print(_col(f"  AI Trading Bot — Analysis Session", "BOLD").center(width + 10))
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Tickers: {', '.join(tickers)}")
    print("=" * width)
    print()


def _print_ticker_result(decision: dict, elapsed: float) -> None:
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


def _print_summary_table(results: list[dict]) -> None:
    sorted_results = sorted(results, key=lambda r: r["decision"]["confidence"], reverse=True)

    col_w = {"ticker": 10, "action": 6, "conf": 6, "risk": 44}
    header = (
        f"  {'TICKER':<{col_w['ticker']}}  "
        f"{'ACTION':<{col_w['action']}}  "
        f"{'CONF':>{col_w['conf']}}  "
        f"{'TOP RISK FLAG':<{col_w['risk']}}"
    )
    divider = "  " + "─" * (sum(col_w.values()) + 8)

    print()
    print("=" * 62)
    print(_col("  SESSION SUMMARY — ranked by confidence", "BOLD"))
    print("=" * 62)
    print(_col(header, "DIM"))
    print(divider)

    for r in sorted_results:
        d        = r["decision"]
        ticker   = d["ticker"]
        action   = d["action"]
        conf     = d["confidence"]
        flags    = d.get("risk_flags", [])
        top_risk = flags[0][:col_w["risk"]] if flags else "—"
        if len(flags[0]) > col_w["risk"] if flags else False:
            top_risk = top_risk[: col_w["risk"] - 1] + "…"

        action_col = _col(f"{action:<{col_w['action']}}", action)
        conf_str   = f"{conf:>{col_w['conf']}.1f}"

        print(
            f"  {ticker:<{col_w['ticker']}}  "
            f"{action_col}  "
            f"{conf_str}  "
            f"{top_risk}"
        )

    print(divider)
    buy_count  = sum(1 for r in results if r["decision"]["action"] == "BUY")
    hold_count = sum(1 for r in results if r["decision"]["action"] == "HOLD")
    sell_count = sum(1 for r in results if r["decision"]["action"] == "SELL")
    print(
        f"  {_col(f'{buy_count} BUY', 'BUY')}  "
        f"{_col(f'{hold_count} HOLD', 'HOLD')}  "
        f"{_col(f'{sell_count} SELL', 'SELL')}"
    )
    print()


def _save_session(results: list[dict], session_ts: str) -> str:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    path = logs_dir / f"session_{session_ts}.json"

    session = {
        "session_id":  session_ts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticker_count": len(results),
        "results": results,
    }

    with open(path, "w") as f:
        json.dump(session, f, indent=2, default=str)

    logger.info("Session saved → %s", path)
    return str(path)


# ---------------------------------------------------------------------------
# Core per-ticker runner
# ---------------------------------------------------------------------------

def run_ticker(ticker: str) -> dict:
    """
    Fetch data and run the full agent pipeline for one ticker.
    Returns a dict: {"ticker", "decision", "market_data", "elapsed_seconds", "error"}.
    """
    from data.fetcher import DataFetcher
    from agents.trading_agents import TradingAgentsWrapper
    from monitoring.logger import log_decision

    t0 = time.monotonic()
    logger.info("Starting analysis for %s", ticker)

    try:
        data = DataFetcher().fetch(ticker)
    except Exception as exc:
        logger.error("[%s] DataFetcher failed: %s", ticker, exc, exc_info=True)
        elapsed = time.monotonic() - t0
        decision = {
            "ticker":      ticker,
            "action":      "HOLD",
            "confidence":  1.0,
            "reasoning":   f"Data fetch failed: {exc}",
            "bull_case":   "",
            "bear_case":   "",
            "risk_flags":  [f"Data unavailable: {exc}"],
            "analysed_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"ticker": ticker, "decision": decision, "market_data": {}, "elapsed_seconds": elapsed, "error": str(exc)}

    try:
        wrapper  = TradingAgentsWrapper()
        decision = wrapper.analyse(data)
    except Exception as exc:
        logger.error("[%s] Agent pipeline failed: %s", ticker, exc, exc_info=True)
        elapsed = time.monotonic() - t0
        decision = {
            "ticker":      ticker,
            "action":      "HOLD",
            "confidence":  1.0,
            "reasoning":   f"Agent pipeline failed: {exc}",
            "bull_case":   "",
            "bear_case":   "",
            "risk_flags":  [f"Pipeline error: {exc}"],
            "analysed_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"ticker": ticker, "decision": decision, "market_data": data, "elapsed_seconds": time.monotonic() - t0, "error": str(exc)}

    elapsed = time.monotonic() - t0
    log_decision(ticker, decision, data)

    return {"ticker": ticker, "decision": decision, "market_data": data, "elapsed_seconds": elapsed, "error": None}


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

    args = parser.parse_args()

    if args.watchlist:
        tickers = _load_watchlist()
    else:
        tickers = [t.upper() for t in args.ticker]

    session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _print_banner(tickers)

    results: list[dict] = []
    failed: list[str]   = []

    for i, ticker in enumerate(tickers, start=1):
        print(f"  [{i}/{len(tickers)}] Analysing {_col(ticker, 'BOLD')}...")
        result = run_ticker(ticker)
        results.append(result)

        _print_ticker_result(result["decision"], result["elapsed_seconds"])

        if result["error"]:
            failed.append(ticker)

    # Summary table
    _print_summary_table(results)

    # Save session JSON
    session_path = _save_session(results, session_ts)
    print(f"  Full session saved → {_col(session_path, 'DIM')}")

    if failed:
        print(f"\n  {_col('Errors:', 'SELL')} {', '.join(failed)} failed — check logs above.\n")
        sys.exit(1)

    print()


if __name__ == "__main__":
    main()
