"""
Unit tests for compute_realised_pnl() in monitoring/logger.py.
Run with: pytest tests/test_pnl_summary.py -v
"""

import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from monitoring.logger import compute_realised_pnl

_SGT = ZoneInfo("Asia/Singapore")

_FIELDS = [
    "timestamp", "ticker", "action", "quantity",
    "price", "order_id", "stop_loss", "take_profit",
]


def _write(csv_path: Path, rows: list) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _row(ticker, action, qty, price, ts="2020-01-01T00:00:00+00:00"):
    return {
        "timestamp": ts, "ticker": ticker, "action": action,
        "quantity": qty, "price": price,
        "order_id": "x", "stop_loss": 0, "take_profit": 0,
    }


def test_missing_csv_returns_zeros(tmp_path):
    today, total = compute_realised_pnl(tmp_path / "nonexistent.csv")
    assert today == 0.0
    assert total == 0.0


def test_empty_csv_returns_zeros(tmp_path):
    p = tmp_path / "trades.csv"
    _write(p, [])
    today, total = compute_realised_pnl(p)
    assert today == 0.0
    assert total == 0.0


def test_open_position_not_counted(tmp_path):
    """BUY with no matching SELL contributes nothing to realised P&L."""
    p = tmp_path / "trades.csv"
    _write(p, [_row("AAPL", "BUY", 10, 100.0)])
    today, total = compute_realised_pnl(p)
    assert today == 0.0
    assert total == 0.0


def test_single_buy_sell_profit(tmp_path):
    """10 shares BUY $100, SELL $120 → P&L +$200."""
    p = tmp_path / "trades.csv"
    _write(p, [
        _row("AAPL", "BUY",  10, 100.0),
        _row("AAPL", "SELL", 10, 120.0),
    ])
    _, total = compute_realised_pnl(p)
    assert abs(total - 200.0) < 0.01


def test_single_buy_sell_loss(tmp_path):
    """10 shares BUY $100, SELL $90 → P&L -$100."""
    p = tmp_path / "trades.csv"
    _write(p, [
        _row("AAPL", "BUY",  10, 100.0),
        _row("AAPL", "SELL", 10,  90.0),
    ])
    _, total = compute_realised_pnl(p)
    assert abs(total - (-100.0)) < 0.01


def test_multiple_tickers_independent(tmp_path):
    """AAPL +$50, MSFT -$100 → total -$50."""
    p = tmp_path / "trades.csv"
    _write(p, [
        _row("AAPL", "BUY",  5,  200.0),
        _row("MSFT", "BUY",  10, 400.0),
        _row("AAPL", "SELL", 5,  210.0),
        _row("MSFT", "SELL", 10, 390.0),
    ])
    _, total = compute_realised_pnl(p)
    assert abs(total - (-50.0)) < 0.01


def test_today_pnl_only_counts_todays_sells(tmp_path):
    """Old sell counts toward total but not today; today's sell counts in both."""
    p = tmp_path / "trades.csv"
    today_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old_ts   = "2020-01-01T00:00:00+00:00"
    _write(p, [
        _row("AAPL", "BUY",  10, 100.0, old_ts),
        _row("AAPL", "SELL", 10, 150.0, old_ts),    # old: +$500
        _row("MSFT", "BUY",  5,  300.0, today_ts),
        _row("MSFT", "SELL", 5,  320.0, today_ts),  # today: +$100
    ])
    today, total = compute_realised_pnl(p)
    assert abs(total - 600.0) < 0.01
    assert abs(today - 100.0) < 0.01


def test_fifo_partial_sell(tmp_path):
    """Sell half of a position at a profit."""
    p = tmp_path / "trades.csv"
    _write(p, [
        _row("AAPL", "BUY",  20, 100.0),
        _row("AAPL", "SELL", 10, 110.0),  # close half: +$100
    ])
    _, total = compute_realised_pnl(p)
    assert abs(total - 100.0) < 0.01


def test_fifo_ordering_multiple_buys(tmp_path):
    """FIFO: first BUY matched to SELL; second BUY remains open."""
    p = tmp_path / "trades.csv"
    _write(p, [
        _row("AAPL", "BUY",  10, 100.0),
        _row("AAPL", "BUY",  10, 200.0),
        _row("AAPL", "SELL", 10, 150.0),  # closes first BUY: +$500
    ])
    _, total = compute_realised_pnl(p)
    assert abs(total - 500.0) < 0.01
