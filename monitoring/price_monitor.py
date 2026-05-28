"""
PriceMonitor — live SL/TP monitor that runs 24/7 on the VPS.

Every 60 seconds:
  1. Fetches all open positions from MooMoo (current_price included)
  2. Looks up stop_loss and take_profit for each ticker from logs/trades.csv
  3. If current_price <= stop_loss:   places market SELL + Telegram alert
  4. If current_price >= take_profit: places market SELL + Telegram alert
  5. Logs each exit to logs/trades.csv

The bracket limit orders placed by scheduler.py are the primary exit mechanism.
This monitor is a software backstop for cases where limit orders don't fill
(e.g. gap-down open, FutuOpenD restart, network hiccup).

Usage:
    python monitoring/price_monitor.py          # run forever (VPS mode)
    python monitoring/price_monitor.py --once   # single check then exit
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("price_monitor")

_SGT          = ZoneInfo("Asia/Singapore")
POLL_INTERVAL = 60    # seconds between position checks
EXIT_COOLDOWN = 300   # seconds before re-checking a just-triggered ticker
TRADES_CSV    = Path("logs/trades.csv")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _futu_to_ticker(futu_code: str) -> str:
    """
    Convert a Futu code back to a standard ticker symbol.

        US.AAPL  → AAPL
        HK.D05   → D05.SI    (SGX ticker — alpha base)
        HK.00700 → 0700.HK   (HK numeric code — zero-padded to 4 chars)
    """
    if futu_code.startswith("US."):
        return futu_code[3:]
    if futu_code.startswith("HK."):
        base = futu_code[3:]
        if base.isdigit():
            return base.lstrip("0").zfill(4) + ".HK"
        return base + ".SI"
    return futu_code


def _load_sl_tp() -> dict:
    """
    Scan logs/trades.csv and return SL/TP for each currently open BUY position.

    Logic: replay the CSV in order — a BUY sets the SL/TP; a matching SELL
    removes it.  Only the most recent BUY survives if a ticker appears multiple
    times.

    Returns:
        { ticker: {"stop_loss": float, "take_profit": float} }
        Tickers with both SL=0 and TP=0 are excluded.
    """
    if not TRADES_CSV.exists():
        logger.debug("[PriceMonitor] trades.csv not found — no SL/TP data")
        return {}

    result: dict = {}
    try:
        with open(TRADES_CSV, newline="") as f:
            for row in csv.DictReader(f):
                ticker = row.get("ticker", "").strip()
                action = row.get("action", "").upper().strip()
                if not ticker or not action:
                    continue
                if action == "BUY":
                    try:
                        sl = float(row.get("stop_loss", 0) or 0)
                        tp = float(row.get("take_profit", 0) or 0)
                    except (ValueError, TypeError):
                        sl, tp = 0.0, 0.0
                    if sl > 0 or tp > 0:
                        result[ticker] = {"stop_loss": sl, "take_profit": tp}
                elif action == "SELL":
                    result.pop(ticker, None)
    except Exception as exc:
        logger.error("[PriceMonitor] Error reading trades.csv: %s", exc)

    return result


def _get_current_price(ticker: str, pos: dict) -> tuple[float, str]:
    """
    Return (price, source) for a position.
    Tries the MooMoo position dict first; falls back to Finnhub if price is 0
    (FutuOpenD returns last_price=0 outside market hours).
    """
    price = float(pos.get("current_price", 0) or 0)
    if price > 0:
        return price, "moomoo"

    api_key = os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        logger.warning("[PriceMonitor] %s — MooMoo price=0 and FINNHUB_API_KEY not set", ticker)
        return 0.0, "none"

    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={api_key}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        price = float(data.get("c", 0) or 0)
        if price > 0:
            return price, "finnhub"
        logger.warning("[PriceMonitor] %s — Finnhub returned price=0", ticker)
    except Exception as exc:
        logger.warning("[PriceMonitor] %s — Finnhub quote failed: %s", ticker, exc)

    return 0.0, "none"


def _log_exit(ticker: str, quantity: int, price: float, order_id: str) -> None:
    """Append a SELL exit row to logs/trades.csv."""
    TRADES_CSV.parent.mkdir(exist_ok=True)
    write_header = not TRADES_CSV.exists()
    try:
        with open(TRADES_CSV, "a", newline="") as f:
            if write_header:
                f.write("timestamp,ticker,action,quantity,price,order_id,stop_loss,take_profit\n")
            f.write(
                f"{datetime.utcnow().isoformat()},{ticker},SELL,{quantity},"
                f"{price},{order_id},0.0,0.0\n"
            )
    except Exception as exc:
        logger.error("[PriceMonitor] Failed to log exit for %s: %s", ticker, exc)


def _now_sgt() -> str:
    return datetime.now(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT")


# ---------------------------------------------------------------------------
# PriceMonitor
# ---------------------------------------------------------------------------

class PriceMonitor:
    """
    Polls MooMoo open positions every POLL_INTERVAL seconds and fires market
    exits when a position's current price breaches its SL or TP level.
    """

    def __init__(self):
        self._triggered: dict[str, float] = {}  # ticker → epoch when exit fired

    def run(self) -> None:
        """Blocking loop — runs forever until killed."""
        logger.info(
            "[PriceMonitor] Starting — polling every %ds  exit cooldown=%ds",
            POLL_INTERVAL, EXIT_COOLDOWN,
        )
        while True:
            try:
                self._check()
            except Exception as exc:
                logger.error("[PriceMonitor] Unhandled cycle error: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL)

    def check_once(self) -> None:
        """Single check cycle — for --once flag / testing."""
        self._check()

    # ------------------------------------------------------------------
    # Core cycle
    # ------------------------------------------------------------------

    def _check(self) -> None:
        now = time.time()

        executor = _make_executor()
        if executor is None:
            logger.warning("[PriceMonitor] No broker available — skipping cycle")
            return

        try:
            positions = executor.get_positions()
        except Exception as exc:
            logger.error("[PriceMonitor] get_positions failed: %s", exc)
            return
        finally:
            _close(executor)

        if not positions:
            logger.info("[PriceMonitor] No open positions")
            return

        sl_tp_map   = _load_sl_tp()
        open_tickers = set()

        for pos in positions:
            raw_code                  = pos.get("ticker", "") or pos.get("code", "")
            ticker                    = _futu_to_ticker(raw_code)
            current_price, price_src  = _get_current_price(ticker, pos)
            quantity                  = int(pos.get("quantity", 0) or 0)

            open_tickers.add(ticker)

            if current_price <= 0 or quantity <= 0:
                continue

            if ticker not in sl_tp_map:
                logger.debug("[PriceMonitor] %s — no SL/TP in trades.csv", ticker)
                continue

            triggered_at = self._triggered.get(ticker)
            if triggered_at and now - triggered_at < EXIT_COOLDOWN:
                remaining = EXIT_COOLDOWN - (now - triggered_at)
                logger.debug(
                    "[PriceMonitor] %s in cooldown (%.0fs left)", ticker, remaining
                )
                continue

            sl = sl_tp_map[ticker]["stop_loss"]
            tp = sl_tp_map[ticker]["take_profit"]

            logger.info(
                "[PriceMonitor] %s  price=$%.2f (source=%-7s)  SL=$%.2f  TP=$%.2f",
                ticker, current_price, price_src, sl, tp,
            )

            if sl > 0 and current_price <= sl:
                self._exit(ticker, quantity, current_price, "STOP LOSS", sl)
            elif tp > 0 and current_price >= tp:
                self._exit(ticker, quantity, current_price, "TAKE PROFIT", tp)

        # Remove cooldown entries for positions that are now closed
        for t in set(self._triggered) - open_tickers:
            del self._triggered[t]
            logger.debug("[PriceMonitor] Cleared cooldown for closed position: %s", t)

    # ------------------------------------------------------------------
    # Exit handler
    # ------------------------------------------------------------------

    def _exit(
        self,
        ticker: str,
        quantity: int,
        current_price: float,
        trigger: str,
        level: float,
    ) -> None:
        """Place market SELL, log the exit, and send a Telegram alert."""
        logger.info(
            "[PriceMonitor] %s triggered — %s × %d @ $%.2f (level=$%.2f)",
            trigger, ticker, quantity, current_price, level,
        )

        order_id = ""
        executor = _make_executor()
        if executor is None:
            logger.warning(
                "[PriceMonitor] No broker — would SELL %s × %d (dry run)", ticker, quantity
            )
        else:
            try:
                order = executor.place_order(
                    ticker=ticker,
                    action="SELL",
                    quantity=quantity,
                    order_type="market",
                )
                if order:
                    order_id = order.get("order_id", "")
                    logger.info(
                        "[PriceMonitor] Exit order placed — id=%s  %s × %d",
                        order_id, ticker, quantity,
                    )
                else:
                    logger.error("[PriceMonitor] Exit order failed for %s", ticker)
            except Exception as exc:
                logger.error("[PriceMonitor] place_order error (%s): %s", ticker, exc)
            finally:
                _close(executor)

        self._triggered[ticker] = time.time()
        _log_exit(ticker, quantity, current_price, order_id)

        icon = "🔴" if trigger == "STOP LOSS" else "🟢"
        _send_telegram(
            f"{icon} <b>{trigger} hit for {ticker}</b>\n"
            f"Price: ${current_price:,.2f}  |  Sold {quantity:,} shares\n"
            f"Order ID: <code>{order_id or '—'}</code>\n"
            f"Time: {_now_sgt()}"
        )


# ---------------------------------------------------------------------------
# Module-level helpers (no state)
# ---------------------------------------------------------------------------

def _make_executor():
    broker = os.getenv("BROKER", "moomoo").lower()
    if broker == "moomoo":
        try:
            from execution.moomoo import MooMooConnector
            return MooMooConnector()
        except Exception as exc:
            logger.error("[PriceMonitor] MooMooConnector init failed: %s", exc)
            return None
    else:
        api_key    = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        if api_key in ("", "your-key-here") or secret_key in ("", "your-key-here"):
            logger.warning("[PriceMonitor] Alpaca keys not configured — no broker")
            return None
        try:
            from execution.alpaca import AlpacaExecutor
            return AlpacaExecutor()
        except Exception as exc:
            logger.error("[PriceMonitor] AlpacaExecutor init failed: %s", exc)
            return None


def _close(executor) -> None:
    try:
        executor.close()
    except Exception:
        pass


def _send_telegram(text: str) -> None:
    try:
        from monitoring.telegram_alerts import TelegramAlerter
        TelegramAlerter()._send(text)
    except Exception as exc:
        logger.warning("[PriceMonitor] Telegram alert failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Trading Bot price monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python monitoring/price_monitor.py           # run forever (VPS mode)
  python monitoring/price_monitor.py --once    # one check then exit
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check cycle then exit (useful for testing)",
    )
    args = parser.parse_args()

    monitor = PriceMonitor()
    if args.once:
        monitor.check_once()
        sys.exit(0)
    monitor.run()


if __name__ == "__main__":
    main()
