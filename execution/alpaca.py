"""
AlpacaExecutor — order execution and account management via Alpaca.

Uses the alpaca-py SDK (alpaca-py>=0.13.0).
Paper trading when TRADING_MODE=paper (default), live when TRADING_MODE=live.
The mode is inferred from both TRADING_MODE and ALPACA_BASE_URL so either
env var alone is sufficient.

Usage:
    executor  = AlpacaExecutor()
    account   = executor.get_account()
    order     = executor.place_order("AAPL", "BUY", 10)
    positions = executor.get_positions()
"""

import logging
import os
from datetime import datetime
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)
_WEEKDAYS = frozenset(range(5))          # Mon=0 … Fri=4


class AlpacaExecutorError(RuntimeError):
    """Raised only during __init__ when credentials are missing."""


class AlpacaExecutor:
    """
    Thin, fault-tolerant wrapper around the Alpaca TradingClient.

    Every public method catches all exceptions, logs them at ERROR level,
    and returns None / [] / False so callers never need to guard for
    API exceptions — just check the return value.
    """

    def __init__(self):
        api_key    = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        base_url     = os.getenv("ALPACA_BASE_URL", "")

        if not api_key or not secret_key:
            raise AlpacaExecutorError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env"
            )

        if api_key in ("your-key-here", "") or secret_key in ("your-key-here", ""):
            logger.warning(
                "[AlpacaExecutor] API keys are placeholder values — "
                "set real Alpaca keys in .env before trading"
            )

        # Infer mode from TRADING_MODE; also accept paper-api base URL as signal
        self._paper: bool = (
            trading_mode == "paper" or "paper-api" in base_url.lower()
        )

        from alpaca.trading.client import TradingClient

        self._client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=self._paper,
        )

        logger.info(
            "[AlpacaExecutor] Initialised — mode=%s",
            "PAPER" if self._paper else "LIVE",
        )

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
    ) -> Optional[dict]:
        """
        Submit a market day order.

        Args:
            ticker:   Stock symbol e.g. "AAPL"
            action:   "BUY" or "SELL"
            quantity: Whole shares (must be >= 1)

        Returns:
            Order dict on success, None on any failure.
        """
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce

        action = action.upper()
        if action not in ("BUY", "SELL"):
            logger.error(
                "[AlpacaExecutor] Invalid action '%s' — must be BUY or SELL", action
            )
            return None
        if quantity < 1:
            logger.error(
                "[AlpacaExecutor] Quantity must be >= 1, got %d", quantity
            )
            return None

        if not self._paper:
            logger.critical(
                "[AlpacaExecutor] LIVE ORDER — %s %s × %d shares — real money at risk",
                action, ticker, quantity,
            )

        logger.info(
            "[AlpacaExecutor] Placing %s market order: %s × %d shares",
            action, ticker, quantity,
        )

        try:
            side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
            request = MarketOrderRequest(
                symbol=ticker,
                qty=float(quantity),
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order  = self._client.submit_order(request)
            result = self._order_to_dict(order)
            logger.info(
                "[AlpacaExecutor] Order submitted — id=%s  status=%s",
                result.get("id"), result.get("status"),
            )
            return result

        except Exception as exc:
            logger.error(
                "[AlpacaExecutor] place_order failed (%s %s × %d): %s",
                action, ticker, quantity, exc, exc_info=True,
            )
            return None

    def cancel_all_orders(self) -> bool:
        """
        Cancel every open order.

        Returns:
            True if the API call succeeded (even if no orders existed),
            False on API error.
        """
        logger.info("[AlpacaExecutor] Cancelling all open orders")
        try:
            statuses  = self._client.cancel_orders()
            cancelled = len(statuses) if statuses else 0
            logger.info("[AlpacaExecutor] Cancelled %d order(s)", cancelled)
            return True
        except Exception as exc:
            logger.error(
                "[AlpacaExecutor] cancel_all_orders failed: %s", exc, exc_info=True
            )
            return False

    def close_position(self, ticker: str) -> Optional[dict]:
        """
        Close an open position at market.

        Args:
            ticker: Stock symbol e.g. "AAPL"

        Returns:
            Order dict on success, None on failure.
        """
        logger.info("[AlpacaExecutor] Closing position: %s", ticker)
        try:
            response = self._client.close_position(ticker)
            result   = self._order_to_dict(response)
            logger.info(
                "[AlpacaExecutor] Close order for %s submitted — id=%s",
                ticker, result.get("id"),
            )
            return result
        except Exception as exc:
            logger.error(
                "[AlpacaExecutor] close_position failed for %s: %s",
                ticker, exc, exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------

    def get_account(self) -> Optional[dict]:
        """
        Fetch current account state.

        Returns:
            { equity, buying_power, cash, portfolio_value, daily_pnl }
            or None on failure.
        """
        logger.debug("[AlpacaExecutor] Fetching account")
        try:
            acct = self._client.get_account()

            equity      = float(acct.equity)
            last_equity = float(acct.last_equity) if acct.last_equity else equity
            daily_pnl   = round(equity - last_equity, 2)

            result = {
                "equity":          round(equity, 2),
                "buying_power":    round(float(acct.buying_power), 2),
                "cash":            round(float(acct.cash), 2),
                "portfolio_value": round(float(acct.portfolio_value), 2),
                "daily_pnl":       daily_pnl,
            }
            logger.info(
                "[AlpacaExecutor] Account — equity=$%.2f  daily_pnl=$%.2f  "
                "buying_power=$%.2f",
                result["equity"], result["daily_pnl"], result["buying_power"],
            )
            return result

        except Exception as exc:
            logger.error(
                "[AlpacaExecutor] get_account failed: %s", exc, exc_info=True
            )
            return None

    def get_positions(self) -> list:
        """
        Fetch all open equity positions.

        Returns:
            List of { ticker, quantity, entry_price, current_price,
                      unrealised_pnl, unrealised_pnl_pct }.
            Empty list on failure.
        """
        logger.debug("[AlpacaExecutor] Fetching open positions")
        try:
            raw    = self._client.get_all_positions()
            result = [self._position_to_dict(p) for p in raw]
            logger.info(
                "[AlpacaExecutor] %d open position(s): %s",
                len(result),
                [p["ticker"] for p in result] if result else "none",
            )
            return result
        except Exception as exc:
            logger.error(
                "[AlpacaExecutor] get_positions failed: %s", exc, exc_info=True
            )
            return []

    def get_order_history(self, limit: int = 20) -> list:
        """
        Fetch recent orders (all statuses), most recent first.

        Args:
            limit: Maximum number of orders to return (default 20).

        Returns:
            List of order dicts, empty list on failure.
        """
        logger.debug("[AlpacaExecutor] Fetching order history (limit=%d)", limit)
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums    import QueryOrderStatus

            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=limit,
            )
            orders = self._client.get_orders(filter=request)
            result = [self._order_to_dict(o) for o in orders]
            logger.info("[AlpacaExecutor] Fetched %d order(s)", len(result))
            return result

        except ImportError:
            # Fallback for SDK versions that don't have QueryOrderStatus.ALL
            logger.debug(
                "[AlpacaExecutor] QueryOrderStatus not available — fetching without status filter"
            )
            try:
                from alpaca.trading.requests import GetOrdersRequest
                orders = self._client.get_orders(filter=GetOrdersRequest(limit=limit))
                return [self._order_to_dict(o) for o in orders]
            except Exception as exc2:
                logger.error(
                    "[AlpacaExecutor] get_order_history fallback failed: %s",
                    exc2, exc_info=True,
                )
                return []

        except Exception as exc:
            logger.error(
                "[AlpacaExecutor] get_order_history failed: %s", exc, exc_info=True
            )
            return []

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def is_market_open(self) -> bool:
        """
        Return True if the US equities market is currently open.

        Uses the Alpaca clock API as the primary source (correctly handles
        holidays and early closes).  Falls back to a local weekday + ET
        time check if the API is unavailable.
        """
        try:
            clock   = self._client.get_clock()
            is_open = bool(clock.is_open)
            logger.debug(
                "[AlpacaExecutor] Alpaca clock — is_open=%s  "
                "next_open=%s  next_close=%s",
                is_open, clock.next_open, clock.next_close,
            )
            return is_open
        except Exception as exc:
            logger.warning(
                "[AlpacaExecutor] Clock API unavailable (%s) — "
                "falling back to local time check",
                exc,
            )
            return self._local_market_open_check()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _local_market_open_check() -> bool:
        """Best-effort fallback: ET weekday + time (does not account for holidays)."""
        now_et = datetime.now(_ET)
        if now_et.weekday() not in _WEEKDAYS:
            return False
        t = now_et.time()
        result = _MARKET_OPEN <= t < _MARKET_CLOSE
        logger.debug(
            "[AlpacaExecutor] Local time check — ET=%s  weekday=%d  open=%s",
            now_et.strftime("%H:%M"), now_et.weekday(), result,
        )
        return result

    @staticmethod
    def _order_to_dict(order) -> dict:
        """Serialise an Alpaca Order / ClosePositionResponse to a plain dict."""
        try:
            side_raw = getattr(order, "side", None)
            side_str = str(side_raw.value if hasattr(side_raw, "value") else side_raw).upper()

            status_raw = getattr(order, "status", None)
            status_str = str(
                status_raw.value if hasattr(status_raw, "value") else status_raw
            )

            filled_price = getattr(order, "filled_avg_price", None)

            return {
                "id":               str(getattr(order, "id", "")),
                "ticker":           str(getattr(order, "symbol", "")),
                "side":             side_str,
                "quantity":         float(getattr(order, "qty", 0) or 0),
                "filled_qty":       float(getattr(order, "filled_qty", 0) or 0),
                "filled_avg_price": float(filled_price) if filled_price is not None else None,
                "type":             str(
                    getattr(order, "order_type",
                            getattr(order, "type", "market"))
                ),
                "status":           status_str,
                "created_at":       str(getattr(order, "created_at", "")),
            }
        except Exception as exc:
            logger.warning(
                "[AlpacaExecutor] Could not serialise order object: %s", exc
            )
            return {}

    @staticmethod
    def _position_to_dict(pos) -> dict:
        """Serialise an Alpaca Position to a plain dict."""
        try:
            # unrealized_plpc is a factor (0.05 = 5%), convert to percentage
            plpc_raw = getattr(pos, "unrealized_plpc", None)
            pnl_pct  = round(float(plpc_raw) * 100, 2) if plpc_raw is not None else 0.0

            current_price_raw = getattr(pos, "current_price", None)

            return {
                "ticker":             str(pos.symbol),
                "quantity":           int(float(pos.qty)),
                "entry_price":        round(float(pos.avg_entry_price), 4),
                "current_price":      (
                    round(float(current_price_raw), 4)
                    if current_price_raw is not None else None
                ),
                "unrealised_pnl":     round(float(pos.unrealized_pl), 2),
                "unrealised_pnl_pct": pnl_pct,
            }
        except Exception as exc:
            logger.warning(
                "[AlpacaExecutor] Could not serialise position object: %s", exc
            )
            return {}
