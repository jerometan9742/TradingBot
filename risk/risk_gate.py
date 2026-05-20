"""
RiskGate — hard guardrails before any trade is placed.

Checks (in order):
  1. Kill switch lock file present   -> BLOCKED
  2. Action is HOLD                  -> pass-through (approved=False, action=HOLD)
  3. Confidence < MIN_CONFIDENCE_SCORE -> BLOCKED
  4. Critical risk flags             -> BLOCKED
  5. Price data missing / stale      -> BLOCKED
  6. Daily loss >= MAX_DAILY_LOSS_PCT -> BLOCKED (auto-activates kill switch)
  7. trades_today >= MAX_TRADES_PER_DAY -> BLOCKED
  8. All checks pass                 -> APPROVED

Usage:
    gate = RiskGate()
    result = gate.check(decision, portfolio_state)
    # portfolio_state: {"equity": float, "daily_pnl": float, "trades_today": int}
"""

import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent / "kill_switch.lock"

_CRITICAL_FLAGS = {"halt", "delist", "fraud", "bankruptcy", "sec investigation"}


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        logger.warning("[RiskGate] Could not parse %s; using default %.4f", key, default)
        return default


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        logger.warning("[RiskGate] Could not parse %s; using default %d", key, default)
        return default


class RiskGate:
    """
    Stateless filter that applies hard risk guardrails to a trading decision.

    All session state (trades_today, daily_pnl, equity) is passed in via
    portfolio_state on each call — RiskGate holds no mutable data between runs.
    """

    def __init__(self):
        self.max_position_size_pct: float = _float_env("MAX_POSITION_SIZE_PCT", 0.05)
        self.max_daily_loss_pct: float    = _float_env("MAX_DAILY_LOSS_PCT", 0.02)
        self.min_confidence: float        = _float_env("MIN_CONFIDENCE_SCORE", 7.0)
        self.max_trades_per_day: int      = _int_env("MAX_TRADES_PER_DAY", 5)

        logger.info(
            "[RiskGate] Loaded params — min_confidence=%.1f  max_trades=%d  "
            "max_daily_loss_pct=%.2f  max_position_size_pct=%.2f",
            self.min_confidence,
            self.max_trades_per_day,
            self.max_daily_loss_pct,
            self.max_position_size_pct,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, decision: dict, portfolio_state: dict) -> dict:
        """
        Evaluate a trade decision against all risk guardrails.

        Args:
            decision:        Output of TradingAgentsWrapper.analyse() —
                             {ticker, action, confidence, risk_flags,
                              reasoning, bull_case, bear_case, analysed_at}
            portfolio_state: Live account snapshot —
                             {equity: float, daily_pnl: float, trades_today: int}

        Returns:
            {approved: bool, action: str, reason: str, original_decision: dict}
        """
        ticker = decision.get("ticker", "UNKNOWN")
        action = str(decision.get("action", "HOLD")).upper()

        logger.info(
            "[RiskGate] Evaluating %s -> %s (confidence %.1f)",
            ticker, action, decision.get("confidence", 0.0),
        )

        blocked_reason = (
            self._check_kill_switch()
            or self._check_hold(action)
            or self._check_confidence(decision)
            or self._check_critical_flags(decision)
            or self._check_price_data(decision)
            or self._check_daily_loss(portfolio_state)
            or self._check_trade_limit(portfolio_state)
        )

        if blocked_reason:
            result = self._build_blocked(action, blocked_reason, decision)
        else:
            result = self._build_approved(action, decision)

        self._log_result(ticker, result)
        return result

    def kill_switch(self) -> None:
        """
        Immediately halt all trading by writing kill_switch.lock to the project root.

        Every subsequent call to check() will be blocked until the file is
        manually removed:  rm kill_switch.lock
        """
        try:
            KILL_SWITCH_FILE.touch()
            logger.critical(
                "[RiskGate] KILL SWITCH ACTIVATED — all trades blocked. "
                "Remove %s to resume.",
                KILL_SWITCH_FILE,
            )
        except OSError as exc:
            logger.error("[RiskGate] Failed to write kill_switch.lock: %s", exc)
            raise

    def reset_daily_counters(self) -> dict:
        """
        Return a portfolio_state fragment with zeroed daily counters.

        Call at market open and merge into your live portfolio_state before
        the first check() of the day.
        """
        reset = {"trades_today": 0, "daily_pnl": 0.0}
        logger.info("[RiskGate] Daily counters reset: %s", reset)
        return reset

    # ------------------------------------------------------------------
    # Guardrail checks — return None (pass) or a reason string (block)
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> Optional[str]:
        if KILL_SWITCH_FILE.exists():
            logger.warning("[RiskGate] Kill switch is active (%s)", KILL_SWITCH_FILE)
            return (
                f"Kill switch active — remove {KILL_SWITCH_FILE} to resume trading"
            )
        return None

    def _check_hold(self, action: str) -> Optional[str]:
        if action == "HOLD":
            logger.debug("[RiskGate] Action is HOLD — no trade to place")
            return "Action is HOLD — nothing to execute"
        return None

    def _check_confidence(self, decision: dict) -> Optional[str]:
        confidence = float(decision.get("confidence", 0.0))
        if confidence < self.min_confidence:
            logger.warning(
                "[RiskGate] Confidence %.1f < %.1f threshold",
                confidence, self.min_confidence,
            )
            return (
                f"Confidence {confidence:.1f} below minimum threshold "
                f"{self.min_confidence:.1f}"
            )
        return None

    def _check_critical_flags(self, decision: dict) -> Optional[str]:
        flags = decision.get("risk_flags", [])
        for flag in flags:
            flag_lower = str(flag).lower()
            for keyword in _CRITICAL_FLAGS:
                if keyword in flag_lower:
                    logger.warning(
                        "[RiskGate] Critical risk flag detected: %r", flag
                    )
                    return f"Critical risk flag: '{flag}'"
        return None

    def _check_price_data(self, decision: dict) -> Optional[str]:
        """Block decisions with no timestamp or that are pipeline-error fallbacks."""
        if not decision.get("analysed_at"):
            logger.warning("[RiskGate] Missing analysed_at — price data may be absent")
            return "Missing price data timestamp (analysed_at)"

        # Pipeline error fallbacks are always: confidence=1.0 + specific reasoning prefix
        if (
            float(decision.get("confidence", 10.0)) == 1.0
            and "Pipeline error" in str(decision.get("reasoning", ""))
        ):
            logger.warning("[RiskGate] Decision is a pipeline error fallback — blocking")
            return "Pipeline error fallback — no valid price data"

        return None

    def _check_daily_loss(self, portfolio_state: dict) -> Optional[str]:
        equity    = float(portfolio_state.get("equity", 0.0))
        daily_pnl = float(portfolio_state.get("daily_pnl", 0.0))

        if equity <= 0:
            logger.warning(
                "[RiskGate] Equity is zero or not provided — skipping daily loss check"
            )
            return None

        if daily_pnl >= 0:
            return None

        loss_pct = abs(daily_pnl) / equity
        if loss_pct >= self.max_daily_loss_pct:
            logger.warning(
                "[RiskGate] Daily loss %.2f%% >= limit %.2f%% — activating kill switch",
                loss_pct * 100, self.max_daily_loss_pct * 100,
            )
            self.kill_switch()
            return (
                f"Daily loss {loss_pct * 100:.2f}% exceeded limit "
                f"{self.max_daily_loss_pct * 100:.2f}% — kill switch activated"
            )
        return None

    def _check_trade_limit(self, portfolio_state: dict) -> Optional[str]:
        trades_today = int(portfolio_state.get("trades_today", 0))
        if trades_today >= self.max_trades_per_day:
            logger.warning(
                "[RiskGate] trades_today=%d >= max=%d",
                trades_today, self.max_trades_per_day,
            )
            return (
                f"Trade limit reached: {trades_today}/{self.max_trades_per_day} "
                f"trades today"
            )
        return None

    # ------------------------------------------------------------------
    # Result builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_approved(action: str, decision: dict) -> dict:
        return {
            "approved":          True,
            "action":            action,
            "reason":            "All risk checks passed",
            "original_decision": decision,
        }

    @staticmethod
    def _build_blocked(original_action: str, reason: str, decision: dict) -> dict:
        # HOLD pass-throughs surface as HOLD; everything else becomes BLOCKED
        out_action = "HOLD" if original_action == "HOLD" else "BLOCKED"
        return {
            "approved":          False,
            "action":            out_action,
            "reason":            reason,
            "original_decision": decision,
        }

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    @staticmethod
    def _log_result(ticker: str, result: dict) -> None:
        status = "APPROVED" if result["approved"] else "BLOCKED"
        logger.info(
            "[RiskGate] %s %s -> %s | %s",
            status, ticker, result["action"], result["reason"],
        )
