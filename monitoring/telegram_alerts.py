"""
Telegram alert sender for the AI trading bot.

Sends formatted notifications directly to the Telegram Bot API via requests.
Silently no-ops if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not configured —
the main pipeline never crashes because of a missing Telegram setup.

Usage (class):
    from monitoring.telegram_alerts import TelegramAlerter
    alerter = TelegramAlerter()
    alerter.send_trade_placed(ticker, action, sizing, order, confidence)

Usage (module-level convenience functions, kept for backward compatibility):
    from monitoring.telegram_alerts import send_trade_placed, send_trade_blocked
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_SGT = ZoneInfo("Asia/Singapore")
_TELEGRAM_TIMEOUT = 10   # seconds


# ---------------------------------------------------------------------------
# TelegramAlerter class
# ---------------------------------------------------------------------------

class TelegramAlerter:
    """
    Sends formatted Telegram alerts for trading events.

    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment at
    construction time.  All send_* methods return True on success and False
    on any failure — they never raise.
    """

    def __init__(self):
        self._token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if self._token and self._chat_id:
            logger.info(
                "[TelegramAlerter] Configured — chat_id=%s", self._chat_id
            )
        else:
            logger.warning(
                "[TelegramAlerter] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set "
                "— all alerts will be silently skipped"
            )

    # ------------------------------------------------------------------
    # Public alert methods
    # ------------------------------------------------------------------

    def send_trade_placed(
        self,
        ticker: str,
        action: str,
        sizing: dict,
        order: dict,
        confidence: float,
        mode: str = "PAPER",
    ) -> bool:
        """Alert: a trade was successfully placed."""
        icon  = "✅" if mode == "LIVE" else "📋"
        qty   = sizing.get("quantity", 0)
        price = sizing.get("entry_price", 0.0)
        val   = sizing.get("position_value", 0.0)
        pct   = sizing.get("position_pct", 0.0) * 100
        sl    = sizing.get("stop_loss_price", 0.0)
        tp    = sizing.get("take_profit_price", 0.0)
        risk  = sizing.get("risk_amount", 0.0)
        oid   = order.get("id", "—")

        text = (
            f"{icon} <b>TRADE PLACED — {mode} MODE</b>\n\n"
            f"<b>{action} {qty} shares of {ticker}</b> @ ${price:,.2f}\n"
            f"Position: ${val:,.0f} ({pct:.1f}% of portfolio)\n"
            f"Stop loss: ${sl:,.2f}  |  Take profit: ${tp:,.2f}\n"
            f"Risk: ${risk:,.2f}  |  Confidence: {confidence:.1f}/10\n\n"
            f"Order ID: <code>{oid}</code>\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def send_trade_blocked(
        self,
        ticker: str,
        action: str,
        reason: str,
        confidence: float,
    ) -> bool:
        """Alert: a trade was blocked by the Risk Gate."""
        text = (
            f"⚠️ <b>TRADE BLOCKED</b>\n\n"
            f"{action} <b>{ticker}</b>  (confidence {confidence:.1f}/10)\n"
            f"Reason: {reason}\n\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def send_cycle_started(self, label: str, tickers: list) -> bool:
        """Alert: an analysis cycle has started."""
        ticker_str = ", ".join(tickers) if tickers else "—"
        text = (
            f"🤖 <b>Analysis cycle started</b>  [{label}]\n"
            f"Tickers: {ticker_str}\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def send_cycle_complete(self, label: str, approved: int, blocked: int) -> bool:
        """Alert: an analysis cycle completed."""
        text = (
            f"✔️ <b>Cycle complete</b>  [{label}]\n"
            f"Trades placed: {approved}  |  Blocked: {blocked}\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def send_kill_switch_triggered(self, reason: str) -> bool:
        """Alert: the kill switch was activated."""
        text = (
            f"🚨 <b>KILL SWITCH ACTIVATED</b>\n\n"
            f"All trading halted.\n"
            f"Reason: {reason}\n"
            f"Time: {_now_sgt()}\n\n"
            f"To resume: <code>rm kill_switch.lock</code>"
        )
        return self._send(text)

    def error_alert(self, error_message: str) -> bool:
        """Alert: an unexpected error occurred in the pipeline."""
        text = (
            f"❌ <b>BOT ERROR</b>\n\n"
            f"{error_message}\n\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def trade_placed(
        self,
        ticker: str,
        action: str,
        quantity: int,
        price: float,
        order_id: str,
    ) -> bool:
        """Alert: a trade was placed (simple signature — key fields only)."""
        icon = "✅"
        text = (
            f"{icon} <b>TRADE PLACED</b>\n\n"
            f"<b>{action} {quantity} shares of {ticker}</b> @ ${price:,.2f}\n"
            f"Order ID: <code>{order_id}</code>\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def trade_blocked(
        self,
        ticker: str,
        action: str,
        confidence: float,
        reason: str,
    ) -> bool:
        """Alert: a trade was blocked by the Risk Gate."""
        text = (
            f"⚠️ <b>TRADE BLOCKED</b>\n\n"
            f"{action} <b>{ticker}</b>  (confidence {confidence:.1f}/10)\n"
            f"Reason: {reason}\n\n"
            f"Time: {_now_sgt()}"
        )
        return self._send(text)

    def daily_summary(self, portfolio_state: dict, decisions: list) -> bool:
        """
        Alert: end-of-day summary of portfolio state and all decisions.

        Args:
            portfolio_state: dict with equity, daily_pnl, trades_today, etc.
            decisions:       list of decision dicts (from TradingAgentsWrapper)
        """
        equity      = portfolio_state.get("equity", 0.0)
        daily_pnl   = portfolio_state.get("daily_pnl", 0.0)
        trades_today = portfolio_state.get("trades_today", 0)
        positions   = portfolio_state.get("open_positions", [])

        pnl_icon = "📈" if daily_pnl >= 0 else "📉"
        pnl_sign = "+" if daily_pnl >= 0 else ""

        lines = [
            f"{pnl_icon} <b>Daily Summary</b>",
            f"",
            f"Equity:       ${equity:,.2f}",
            f"Daily P&L:    {pnl_sign}${daily_pnl:,.2f}",
            f"Trades today: {trades_today}",
            f"Open positions: {len(positions)}",
        ]

        if decisions:
            lines.append("")
            lines.append("<b>Decisions:</b>")
            for d in decisions:
                ticker = d.get("ticker", "?")
                action = d.get("action", "?")
                conf   = d.get("confidence", 0.0)
                lines.append(f"  {ticker}: {action} ({conf:.1f}/10)")

        lines.append(f"\nTime: {_now_sgt()}")
        return self._send("\n".join(lines))

    def kill_switch_activated(self, reason: str) -> bool:
        """Alert: the kill switch was activated (alias for send_kill_switch_triggered)."""
        return self.send_kill_switch_triggered(reason)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send(self, text: str) -> bool:
        """POST to the Telegram Bot API. Returns True on success, False otherwise."""
        if not self._token or not self._chat_id:
            logger.debug("[TelegramAlerter] Credentials not set — alert skipped")
            return False

        url     = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}

        try:
            resp = requests.post(url, json=payload, timeout=_TELEGRAM_TIMEOUT)
            if resp.ok:
                logger.debug("[TelegramAlerter] Sent (chat_id=%s)", self._chat_id)
                return True
            logger.warning(
                "[TelegramAlerter] API %d: %s", resp.status_code, resp.text[:200]
            )
            return False
        except requests.exceptions.Timeout:
            logger.warning(
                "[TelegramAlerter] Timed out after %ds", _TELEGRAM_TIMEOUT
            )
            return False
        except Exception as exc:
            logger.warning("[TelegramAlerter] Send failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Module-level convenience functions (backward-compatible with scheduler.py)
# Each call creates a fresh TelegramAlerter so credentials are always current.
# ---------------------------------------------------------------------------

def _alerter() -> TelegramAlerter:
    return TelegramAlerter()


def send_trade_placed(
    ticker: str,
    action: str,
    sizing: dict,
    order: dict,
    confidence: float,
    mode: str = "PAPER",
) -> bool:
    return _alerter().send_trade_placed(ticker, action, sizing, order, confidence, mode)


def send_trade_blocked(
    ticker: str,
    action: str,
    reason: str,
    confidence: float,
) -> bool:
    return _alerter().send_trade_blocked(ticker, action, reason, confidence)


def send_cycle_started(label: str, tickers: list) -> bool:
    return _alerter().send_cycle_started(label, tickers)


def send_cycle_complete(label: str, approved: int, blocked: int) -> bool:
    return _alerter().send_cycle_complete(label, approved, blocked)


def send_kill_switch_triggered(reason: str) -> bool:
    return _alerter().send_kill_switch_triggered(reason)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _now_sgt() -> str:
    return datetime.now(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
