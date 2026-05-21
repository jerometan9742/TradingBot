"""
Structured trade and decision logger.
Writes every decision (with inputs) to JSON for reproducibility review.
"""

import json
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


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
