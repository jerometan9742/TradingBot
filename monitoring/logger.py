"""
Structured trade and decision logger.
Writes every decision (with inputs) to JSON for reproducibility review.
"""

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_TRADES_CSV = LOG_DIR / "trades.csv"
_SGT = ZoneInfo("Asia/Singapore")


def log_decision(ticker: str, decision: dict, market_data: dict) -> str:
    """
    Log a full trade decision with all inputs.
    Returns the log file path.
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = LOG_DIR / f"{ticker}_{timestamp}.json"

    record = {
        "logged_at": datetime.utcnow().isoformat(),
        "ticker": ticker,
        "decision": decision,
        "market_data_snapshot": market_data,
    }

    with open(filename, "w") as f:
        json.dump(record, f, indent=2, default=str)

    print(f"[Logger] Decision logged: {filename}")
    return str(filename)


def log_trade(
    ticker: str,
    action: str,
    quantity: float,
    price: float,
    order_id: str,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
) -> None:
    """Log a placed trade to a running CSV."""
    trade_log = LOG_DIR / "trades.csv"
    write_header = not trade_log.exists()

    with open(trade_log, "a") as f:
        if write_header:
            f.write("timestamp,ticker,action,quantity,price,order_id,stop_loss,take_profit\n")
        f.write(
            f"{datetime.utcnow().isoformat()},{ticker},{action},{quantity},{price},"
            f"{order_id},{stop_loss},{take_profit}\n"
        )


def compute_realised_pnl(csv_path=None, tz=None):
    """
    FIFO-match BUY/SELL rows in trades.csv and return
    (today_realised_pnl, total_realised_pnl).

    Args:
        csv_path: Path to trades.csv (defaults to LOG_DIR/trades.csv).
        tz:       Timezone for "today" comparison (defaults to Asia/Singapore).

    Returns:
        (today_realised, total_realised) — both floats, 0.0 on any read error.
    """
    if tz is None:
        tz = _SGT
    if csv_path is None:
        csv_path = _TRADES_CSV

    if not Path(csv_path).exists():
        return 0.0, 0.0

    today = datetime.now(tz).date()
    rows: list = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:
        return 0.0, 0.0

    buy_queue: dict = {}
    today_realised = 0.0
    total_realised = 0.0

    for row in rows:
        action = (row.get("action") or "").upper().strip()
        ticker = (row.get("ticker") or "").strip()
        try:
            qty   = float(row.get("quantity") or 0)
            price = float(row.get("price") or 0)
        except (ValueError, TypeError):
            continue
        if qty <= 0 or not ticker or price <= 0:
            continue

        if action == "BUY":
            buy_queue.setdefault(ticker, []).append((qty, price))
        elif action == "SELL":
            buys      = buy_queue.get(ticker, [])
            remaining = qty
            cost      = 0.0
            used      = 0.0
            while buys and remaining > 0:
                bqty, bprice = buys[0]
                take      = min(bqty, remaining)
                cost     += take * bprice
                used     += take
                remaining -= take
                if take == bqty:
                    buys.pop(0)
                else:
                    buys[0] = (bqty - take, bprice)
            if used > 0:
                pnl            = used * price - cost
                total_realised += pnl
                ts_str = row.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts.astimezone(tz).date() == today:
                        today_realised += pnl
                except Exception:
                    pass

    return today_realised, total_realised
