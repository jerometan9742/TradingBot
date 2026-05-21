"""
PositionSizer — fixed-fractional position sizing with confidence scaling.

Sizing formula:
    risk_amount      = portfolio_value * risk_per_trade_pct
    raw_quantity     = risk_amount / (entry_price * stop_loss_pct)
    scaled_quantity  = raw_quantity * confidence_multiplier
    capped_quantity  = min(scaled_quantity, max_position_value / entry_price)
    final_quantity   = floor(capped_quantity)   # whole shares only

Confidence multipliers:
    7.0 – 7.9  →  50% of max position
    8.0 – 8.9  →  75% of max position
    9.0 – 10.0 → 100% of max position

Guardrails:
    - position_value <= portfolio_value * MAX_POSITION_SIZE_PCT
    - position_value >= MIN_TRADE_VALUE ($100)
    - quantity >= 1 whole share

Usage:
    sizer = PositionSizer()
    sizing = sizer.calculate("AAPL", "BUY", 185.50, 100_000, 8.3)
"""

import logging
import math
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_CONFIDENCE_TIERS = [
    (9.0, 1.00),
    (8.0, 0.75),
    (7.0, 0.50),
]

MIN_TRADE_VALUE = 100.0  # reject positions smaller than this


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        logger.warning("[PositionSizer] Could not parse %s; using default %.4f", key, default)
        return default


class PositionSizerError(ValueError):
    """Raised when a valid position cannot be constructed."""


class PositionSizer:
    """
    Calculates fixed-fractional position sizes, capped and confidence-scaled.

    All parameters are loaded from .env at construction time so the object
    is reusable across the full watchlist without re-reading the environment.
    """

    def __init__(self):
        self.max_position_size_pct: float = _float_env("MAX_POSITION_SIZE_PCT", 0.05)
        self.risk_per_trade_pct: float    = _float_env("RISK_PER_TRADE_PCT", 0.01)
        self.stop_loss_pct: float         = _float_env("STOP_LOSS_PCT", 0.03)
        self.take_profit_multiplier: float = _float_env("TAKE_PROFIT_MULTIPLIER", 2.0)

        logger.info(
            "[PositionSizer] Loaded params — max_position_size_pct=%.2f  "
            "risk_per_trade_pct=%.2f  stop_loss_pct=%.2f  take_profit_mult=%.1f",
            self.max_position_size_pct,
            self.risk_per_trade_pct,
            self.stop_loss_pct,
            self.take_profit_multiplier,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(
        self,
        ticker: str,
        action: str,
        entry_price: float,
        portfolio_value: float,
        confidence: float,
        market_data: dict | None = None,
    ) -> dict:
        """
        Calculate a position size for a proposed trade.

        Args:
            ticker:          Stock symbol, e.g. "AAPL"
            action:          "BUY" or "SELL"
            entry_price:     Current market price per share
            portfolio_value: Fallback equity if live MooMoo fetch fails
            confidence:      Agent confidence score, 7.0 – 10.0
            market_data:     Optional unified data dict from DataFetcher;
                             used to extract ATR for dynamic SL/TP levels.

        Returns:
            Sizing dict (see module docstring for full schema).

        Raises:
            PositionSizerError: if inputs are invalid or the position
                                would be too small to place.
        """
        action = action.upper()
        self._validate_inputs(ticker, action, entry_price, portfolio_value, confidence)

        # Prefer live US account equity from MooMoo; fall back to passed-in value
        live_pv = self._live_portfolio_value()
        if live_pv is not None:
            logger.info(
                "[PositionSizer] Live MooMoo US equity: $%.2f (fallback was $%.2f)",
                live_pv, portfolio_value,
            )
            portfolio_value = live_pv
        else:
            logger.info(
                "[PositionSizer] MooMoo unavailable — using fallback portfolio_value: $%.2f",
                portfolio_value,
            )

        logger.info(
            "[PositionSizer] Sizing %s %s @ $%.2f  portfolio=$%.2f  confidence=%.1f",
            action, ticker, entry_price, portfolio_value, confidence,
        )

        # --- limits and scaling ------------------------------------------
        max_position_value  = portfolio_value * self.max_position_size_pct
        confidence_mult     = self._confidence_multiplier(confidence)
        scaled_max_value    = max_position_value * confidence_mult

        logger.debug(
            "[PositionSizer] max_position_value=$%.2f  confidence_mult=%.2f  "
            "scaled_max=$%.2f",
            max_position_value, confidence_mult, scaled_max_value,
        )

        # --- fixed-fractional sizing -------------------------------------
        risk_amount  = portfolio_value * self.risk_per_trade_pct
        raw_quantity = risk_amount / (entry_price * self.stop_loss_pct)

        logger.debug(
            "[PositionSizer] risk_amount=$%.2f  raw_quantity=%.4f",
            risk_amount, raw_quantity,
        )

        # --- apply confidence cap and position cap -----------------------
        capped_by_max = scaled_max_value / entry_price
        final_quantity = math.floor(min(raw_quantity, capped_by_max))

        logger.debug(
            "[PositionSizer] capped_by_max=%.4f  floor=%d",
            capped_by_max, final_quantity,
        )

        # --- guardrails --------------------------------------------------
        if final_quantity < 1:
            raise PositionSizerError(
                f"{ticker}: position size rounds to 0 shares "
                f"(entry=${entry_price:.2f}, portfolio=${portfolio_value:.2f}, "
                f"confidence={confidence:.1f})"
            )

        position_value = final_quantity * entry_price
        if position_value < MIN_TRADE_VALUE:
            raise PositionSizerError(
                f"{ticker}: position value ${position_value:.2f} is below "
                f"minimum trade size ${MIN_TRADE_VALUE:.2f}"
            )

        # --- stop-loss and take-profit (ATR-based when available) --------
        atr_value = None
        if market_data:
            atr_value = (market_data.get("technicals") or {}).get("atr_14")
            if atr_value:
                logger.info(
                    "[PositionSizer] ATR-14 for %s: %.4f — using dynamic SL/TP",
                    ticker, atr_value,
                )
            else:
                logger.info(
                    "[PositionSizer] ATR not available for %s — using fixed-pct SL/TP",
                    ticker,
                )

        stop_loss_price, take_profit_price = self._levels(
            action, entry_price, atr_value=atr_value
        )

        result = {
            "ticker":             ticker,
            "action":             action,
            "entry_price":        round(entry_price, 4),
            "quantity":           final_quantity,
            "position_value":     round(position_value, 2),
            "position_pct":       round(position_value / portfolio_value, 6),
            "stop_loss_price":    round(stop_loss_price, 4),
            "take_profit_price":  round(take_profit_price, 4),
            "stop_loss_pct":      round(abs(entry_price - stop_loss_price) / entry_price, 6),
            "take_profit_pct":    round(abs(take_profit_price - entry_price) / entry_price, 6),
            "atr_14":             round(atr_value, 4) if atr_value else None,
            "risk_amount":        round(risk_amount, 2),
            "max_position_value": round(max_position_value, 2),
        }

        logger.info(
            "[PositionSizer] %s %s: qty=%d  value=$%.2f (%.1f%%)  "
            "SL=$%.2f  TP=$%.2f  risk=$%.2f",
            action, ticker,
            result["quantity"],
            result["position_value"],
            result["position_pct"] * 100,
            result["stop_loss_price"],
            result["take_profit_price"],
            result["risk_amount"],
        )
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _live_portfolio_value(self) -> Optional[float]:
        """
        Fetch US market total_assets (USD) from MooMoo as live equity.

        Only attempted when BROKER=moomoo. Returns None if FutuOpenD is
        unreachable, trade is not unlocked, or the US balance is zero —
        callers fall back to the passed-in portfolio_value in all these cases.
        """
        if os.getenv("BROKER", "moomoo").lower() != "moomoo":
            return None
        try:
            from execution.moomoo import MooMooConnector
            with MooMooConnector() as conn:
                balance = conn.get_account_balance()
            value = float(
                balance.get("by_market", {}).get("US", {}).get("total_assets", 0) or 0
            )
            return value if value > 0 else None
        except Exception as exc:
            logger.warning("[PositionSizer] MooMoo equity fetch failed: %s", exc)
            return None

    def _levels(
        self,
        action: str,
        entry_price: float,
        atr_value: Optional[float] = None,
    ):
        """Return (stop_loss_price, take_profit_price) for BUY or SELL.

        ATR-based when atr_value is provided (SL = 1.5×ATR, TP = 3.0×ATR).
        Falls back to fixed percentages from .env otherwise.
        """
        if atr_value and atr_value > 0:
            stop_distance = atr_value * 1.5
            tp_distance   = atr_value * 3.0
        else:
            stop_distance = entry_price * self.stop_loss_pct
            tp_distance   = stop_distance * self.take_profit_multiplier

        if action == "BUY":
            stop_loss_price   = entry_price - stop_distance
            take_profit_price = entry_price + tp_distance
        else:  # SELL / short
            stop_loss_price   = entry_price + stop_distance
            take_profit_price = entry_price - tp_distance

        return stop_loss_price, take_profit_price

    @staticmethod
    def _confidence_multiplier(confidence: float) -> float:
        """Map confidence score to a position-size fraction."""
        for threshold, multiplier in _CONFIDENCE_TIERS:
            if confidence >= threshold:
                logger.debug(
                    "[PositionSizer] confidence=%.1f -> multiplier=%.2f",
                    confidence, multiplier,
                )
                return multiplier
        # Below 7.0 — callers should not reach here (RiskGate blocks < 7.0)
        logger.warning(
            "[PositionSizer] confidence=%.1f is below 7.0; using 50%% size",
            confidence,
        )
        return 0.50

    @staticmethod
    def _validate_inputs(
        ticker: str,
        action: str,
        entry_price: float,
        portfolio_value: float,
        confidence: float,
    ) -> None:
        errors = []

        if not ticker or not isinstance(ticker, str):
            errors.append("ticker must be a non-empty string")

        if action not in ("BUY", "SELL"):
            errors.append(f"action must be BUY or SELL, got '{action}'")

        if not isinstance(entry_price, (int, float)) or entry_price <= 0:
            errors.append(f"entry_price must be > 0, got {entry_price!r}")

        if not isinstance(portfolio_value, (int, float)) or portfolio_value <= 0:
            errors.append(f"portfolio_value must be > 0, got {portfolio_value!r}")

        if not isinstance(confidence, (int, float)) or not (1.0 <= confidence <= 10.0):
            errors.append(f"confidence must be 1.0–10.0, got {confidence!r}")

        if errors:
            raise PositionSizerError(
                f"[PositionSizer] Input validation failed: {'; '.join(errors)}"
            )
