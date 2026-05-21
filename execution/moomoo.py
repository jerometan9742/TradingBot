"""
MooMooConnector — order execution and account management via Futu OpenAPI.

Requires FutuOpenD gateway running locally (download from futunn.com/download/OpenAPI).
Uses the futu-api Python SDK (pip install futu-api).

Paper trading:  TRADING_MODE=paper  → TrdEnv.SIMULATE
Live trading:   TRADING_MODE=live   → TrdEnv.REAL

Ticker routing:
    US stocks  (AAPL)    → code "US.AAPL",   TrdMarket.US
    SGX stocks (D05.SI)  → code "HK.D05",    TrdMarket.HK  (Futu routes SGX via HK)
    HK stocks  (0700.HK) → code "HK.00700",  TrdMarket.HK
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Ticker suffixes that route through the Futu HK market context
_HK_SUFFIXES = frozenset({".SI", ".HK"})


class MooMooConnectorError(RuntimeError):
    """Raised when FutuOpenD is unreachable or credentials are invalid."""


class MooMooConnector:
    """
    Thin wrapper around Futu OpenAPI for multi-market order execution.

    Two OpenSecTradeContext instances are lazily maintained:
        _ctx_hk  — TrdMarket.HK (handles HK stocks + SGX via HK routing)
        _ctx_us  — TrdMarket.US

    Both contexts connect to the same FutuOpenD gateway process and are
    only opened when a trade or query for that market is first requested.
    Call close() or use as a context manager to release connections.
    """

    def __init__(self):
        import futu as ft  # imported here so module loads even if futu-api is absent
        self._ft = ft

        self._host = os.getenv("MOOMOO_HOST", "127.0.0.1")
        self._port = int(os.getenv("MOOMOO_PORT", "11111"))

        trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        self._trd_env = ft.TrdEnv.SIMULATE if trading_mode == "paper" else ft.TrdEnv.REAL
        self.trading_mode = "paper" if self._trd_env == ft.TrdEnv.SIMULATE else "live"

        # Lazily opened — None until first use for that market
        self._ctx_hk: Optional[object] = None
        self._ctx_us: Optional[object] = None

        logger.info(
            "[MooMoo] Initialised — host=%s:%d  mode=%s",
            self._host, self._port, self.trading_mode.upper(),
        )

    def __repr__(self) -> str:
        return (
            f"MooMooConnector(host={self._host}:{self._port}, "
            f"mode={self.trading_mode.upper()})"
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        self._close_contexts()

    def close(self):
        """Explicitly close all open FutuOpenD connections."""
        self._close_contexts()
        logger.info("[MooMoo] Connections closed")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _close_contexts(self):
        for ctx in (self._ctx_hk, self._ctx_us):
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
        self._ctx_hk = None
        self._ctx_us = None

    def _is_hk_market(self, ticker: str) -> bool:
        upper = ticker.upper()
        return any(upper.endswith(s) for s in _HK_SUFFIXES)

    def _futu_code(self, ticker: str) -> str:
        """
        Convert a standard ticker symbol to Futu code format.

            AAPL    → US.AAPL
            D05.SI  → HK.D05
            0700.HK → HK.00700  (zero-padded to 5 digits for HK stocks)
        """
        upper = ticker.upper()
        if upper.endswith(".SI"):
            return "HK." + upper[:-3]
        if upper.endswith(".HK"):
            base = upper[:-3]
            return "HK." + base.zfill(5)
        return "US." + upper

    def _get_ctx(self, market: str):
        """
        Return (and lazily open) the context for 'HK' or 'US'.
        Raises MooMooConnectorError if FutuOpenD is unreachable.
        """
        ft = self._ft
        if market == "HK":
            if self._ctx_hk is None:
                try:
                    self._ctx_hk = ft.OpenSecTradeContext(
                        filter_trdmarket=ft.TrdMarket.HK,
                        host=self._host,
                        port=self._port,
                    )
                except Exception as exc:
                    raise MooMooConnectorError(
                        f"Cannot connect to FutuOpenD at {self._host}:{self._port} "
                        f"(HK context): {exc}"
                    ) from exc
            return self._ctx_hk
        else:
            if self._ctx_us is None:
                try:
                    self._ctx_us = ft.OpenSecTradeContext(
                        filter_trdmarket=ft.TrdMarket.US,
                        host=self._host,
                        port=self._port,
                    )
                except Exception as exc:
                    raise MooMooConnectorError(
                        f"Cannot connect to FutuOpenD at {self._host}:{self._port} "
                        f"(US context): {exc}"
                    ) from exc
            return self._ctx_us

    def _ctx_for_ticker(self, ticker: str):
        """Return the right context based on ticker suffix."""
        return self._get_ctx("HK" if self._is_hk_market(ticker) else "US")

    # ── Order execution ───────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        order_type: str = "market",
        price: float = 0.0,
    ) -> Optional[dict]:
        """
        Submit an order via FutuOpenD.

        Args:
            ticker:     Stock symbol e.g. "AAPL" or "D05.SI"
            action:     "buy" or "sell" (case-insensitive)
            quantity:   Whole shares (must be >= 1)
            order_type: "market" (default) or "limit"
            price:      Limit price; ignored for market orders (use 0.0)

        Returns:
            { order_id, ticker, code, action, quantity, order_type, status }
            or None on any failure.
        """
        ft = self._ft
        action = action.lower()

        if action not in ("buy", "sell"):
            logger.error("[MooMoo] Invalid action '%s' — must be buy or sell", action)
            return None
        if quantity < 1:
            logger.error("[MooMoo] Quantity must be >= 1, got %d", quantity)
            return None

        if self._trd_env == ft.TrdEnv.REAL:
            logger.critical(
                "[MooMoo] LIVE ORDER — %s %s × %d shares — real money at risk",
                action.upper(), ticker, quantity,
            )

        code = self._futu_code(ticker)
        side = ft.TrdSide.BUY if action == "buy" else ft.TrdSide.SELL
        ot   = ft.OrderType.MARKET if order_type == "market" else ft.OrderType.NORMAL

        logger.info(
            "[MooMoo] Placing %s %s: %s × %d shares (code=%s env=%s)",
            order_type.upper(), action.upper(), ticker, quantity,
            code, self.trading_mode.upper(),
        )

        try:
            ctx = self._ctx_for_ticker(ticker)
            ret, data = ctx.place_order(
                price=price,
                qty=float(quantity),
                code=code,
                trd_side=side,
                order_type=ot,
                trd_env=self._trd_env,
            )
            if ret != ft.RET_OK:
                logger.error("[MooMoo] place_order API error for %s: %s", ticker, data)
                return None

            row = data.iloc[0].to_dict() if (hasattr(data, "iloc") and len(data) > 0) else {}
            result = {
                "order_id":   str(row.get("order_id", "")),
                "ticker":     ticker,
                "code":       code,
                "action":     action.upper(),
                "quantity":   quantity,
                "order_type": order_type,
                "status":     str(row.get("order_status", "")),
            }
            logger.info(
                "[MooMoo] Order placed — id=%s  status=%s",
                result["order_id"], result["status"],
            )
            return result

        except MooMooConnectorError:
            raise
        except Exception as exc:
            logger.error(
                "[MooMoo] place_order exception (%s %s × %d): %s",
                action.upper(), ticker, quantity, exc, exc_info=True,
            )
            return None

    def get_order_status(self, order_id: str, ticker: str) -> dict:
        """
        Query the current fill status of an open or recently filled order.

        Args:
            order_id: Futu order ID returned by place_order
            ticker:   Stock symbol — used to select the correct market context

        Returns:
            {
                "order_id":   str,
                "status":     str,   # e.g. "FILLED_ALL", "SUBMITTED", "FAILED"
                "fill_price": float, # average fill price; 0.0 if not yet filled
                "fill_qty":   int,   # shares filled so far
            }
        """
        ft     = self._ft
        _empty = {
            "order_id":   order_id,
            "status":     "UNKNOWN",
            "fill_price": 0.0,
            "fill_qty":   0,
        }
        try:
            ctx = self._ctx_for_ticker(ticker)
            ret, data = ctx.order_list_query(
                order_id=order_id,
                trd_env=self._trd_env,
            )
            if ret != ft.RET_OK:
                logger.warning(
                    "[MooMoo] order_list_query failed (id=%s): %s", order_id, data
                )
                return _empty

            if not (hasattr(data, "iloc") and len(data) > 0):
                return _empty

            row        = data.iloc[0].to_dict()
            status     = str(row.get("order_status", "UNKNOWN"))
            fill_price = float(row.get("dealt_avg_price", 0) or 0)
            fill_qty   = int(float(row.get("dealt_qty", 0) or 0))

            logger.debug(
                "[MooMoo] Order %s — status=%s  fill_price=%.4f  fill_qty=%d",
                order_id, status, fill_price, fill_qty,
            )
            return {
                "order_id":   order_id,
                "status":     status,
                "fill_price": fill_price,
                "fill_qty":   fill_qty,
            }

        except Exception as exc:
            logger.warning(
                "[MooMoo] get_order_status error (id=%s): %s", order_id, exc
            )
            return _empty

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a single open order by ID.

        Tries both HK and US contexts since we may not know which market
        the original order was for.

        Args:
            order_id: Futu order ID (returned by place_order)

        Returns:
            True on success, False if not found or API error.
        """
        ft = self._ft
        logger.info("[MooMoo] Cancelling order %s", order_id)

        for market in ("HK", "US"):
            try:
                ctx = self._get_ctx(market)
                ret, data = ctx.modify_order(
                    modify_order_op=ft.ModifyOrderOp.CANCEL,
                    order_id=order_id,
                    qty=0,
                    price=0,
                    trd_env=self._trd_env,
                )
                if ret == ft.RET_OK:
                    logger.info("[MooMoo] Order %s cancelled via %s context", order_id, market)
                    return True
                logger.debug("[MooMoo] cancel via %s failed (may not be this market): %s", market, data)
            except MooMooConnectorError as exc:
                logger.debug("[MooMoo] %s context unavailable for cancel: %s", market, exc)
            except Exception as exc:
                logger.warning("[MooMoo] cancel_order error on %s context: %s", market, exc)

        logger.error("[MooMoo] cancel_order: order %s not found on any market", order_id)
        return False

    # ── Account queries ───────────────────────────────────────────────────────

    def get_account_balance(self) -> dict:
        """
        Fetch cash and portfolio value across all markets.

        Queries both HK (USD denomination) and US contexts and sums the totals.
        Individual market values are also returned for transparency.

        Returns:
            {
                "cash":            float,
                "market_value":    float,
                "portfolio_value": float,
                "by_market":       { "HK": {...}, "US": {...} }
            }
        """
        ft = self._ft
        totals = {"cash": 0.0, "market_value": 0.0, "portfolio_value": 0.0, "by_market": {}}

        for market, currency in (("US", "USD"), ("HK", "USD")):
            try:
                ctx = self._get_ctx(market)
                ret, data = ctx.accinfo_query(
                    trd_env=self._trd_env,
                    currency=currency,
                )
                if ret != ft.RET_OK:
                    logger.warning("[MooMoo] accinfo_query failed for %s: %s", market, data)
                    continue

                if hasattr(data, "iloc") and len(data) > 0:
                    row = data.iloc[0]
                    cash     = float(row.get("cash", 0) or 0)
                    mkt_val  = float(row.get("market_val", 0) or 0)
                    total    = float(row.get("total_assets", 0) or 0)
                    totals["cash"]            += cash
                    totals["market_value"]    += mkt_val
                    totals["portfolio_value"] += total
                    totals["by_market"][market] = {
                        "cash": cash, "market_value": mkt_val, "total_assets": total,
                        "currency": currency,
                    }

            except MooMooConnectorError as exc:
                logger.warning("[MooMoo] %s context unavailable for balance: %s", market, exc)
            except Exception as exc:
                logger.warning("[MooMoo] get_account_balance error for %s: %s", market, exc, exc_info=True)

        logger.info(
            "[MooMoo] Balance — cash=%.2f  market_value=%.2f  portfolio=%.2f",
            totals["cash"], totals["market_value"], totals["portfolio_value"],
        )
        return totals

    def get_positions(self) -> list:
        """
        Fetch all open positions across HK and US markets.

        Returns:
            List of {
                ticker, code, quantity, entry_price, current_price,
                unrealised_pnl, unrealised_pnl_pct, market
            }.
            Empty list on failure.
        """
        ft = self._ft
        positions = []

        for market in ("HK", "US"):
            try:
                ctx = self._get_ctx(market)
                ret, data = ctx.position_list_query(trd_env=self._trd_env)
                if ret != ft.RET_OK:
                    logger.warning("[MooMoo] position_list_query failed for %s: %s", market, data)
                    continue

                if hasattr(data, "iterrows"):
                    for _, row in data.iterrows():
                        pl_ratio = float(row.get("pl_ratio", 0) or 0)
                        positions.append({
                            "ticker":             str(row.get("code", "")),
                            "code":               str(row.get("code", "")),
                            "quantity":           int(float(row.get("qty", 0) or 0)),
                            "entry_price":        float(row.get("cost_price", 0) or 0),
                            "current_price":      float(row.get("last_price", 0) or 0),
                            "unrealised_pnl":     float(row.get("pl_val", 0) or 0),
                            "unrealised_pnl_pct": pl_ratio * 100,
                            "market":             market,
                        })

            except MooMooConnectorError as exc:
                logger.warning("[MooMoo] %s context unavailable for positions: %s", market, exc)
            except Exception as exc:
                logger.warning("[MooMoo] get_positions error for %s: %s", market, exc, exc_info=True)

        logger.info(
            "[MooMoo] %d open position(s): %s",
            len(positions),
            [p["ticker"] for p in positions] if positions else "none",
        )
        return positions
