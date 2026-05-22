"""
scheduler.py — APScheduler-driven pipeline runner for the AI trading bot.

Schedule (SGT / Asia/Singapore — all Mon-Fri):
    18:00  universe_update_cycle()           — auto-refresh watchlist_universe.txt
    19:00  reset_daily_state()               — clear counters before session
    19:00  run_daily_screen()                — momentum screener, top-15 → Telegram
    20:00  run_analysis_cycle("pre_market")  — pre-market scan, min confidence 8.5
    21:30  run_analysis_cycle("ny_open")     — NYSE open, standard confidence 7.0
    03:00  run_analysis_cycle("afternoon")   — afternoon momentum, confidence 7.0
    04:00  market_close_cycle()              — P&L summary only, no new trades

Pre-market behaviour (20:00):
    - Sends "📊 Pre-market scan: {ticker} → {signal} (confidence {score}/10)" per ticker
    - Only places trades when confidence >= 8.5
    - Sends "⚡ High confidence pre-market trade placed" if a trade goes through

Background order monitor:
    - Daemon thread checks _pending_orders every 30 s via MooMooConnector
    - On FILLED:    Telegram + log to trades.csv
    - On CANCELLED: Telegram + log to trades.csv
    - On REJECTED:  Telegram + log to trades.csv

Usage:
    python scheduler.py              # start the scheduler (blocking)
    python scheduler.py --run-now    # one immediate cycle then exit
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("scheduler")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SGT                    = ZoneInfo("Asia/Singapore")
_TZ_SCHED               = "Asia/Singapore"
_STANDARD_CONFIDENCE    = 7.0    # default minimum for placing trades
_PREMARKET_CONFIDENCE   = 8.5    # higher bar for pre-market session
_ORDER_MONITOR_INTERVAL = 30     # seconds between background order-status polls


# ---------------------------------------------------------------------------
# TradingScheduler
# ---------------------------------------------------------------------------

class TradingScheduler:
    """
    Orchestrates the full analysis → risk → execution pipeline on a cron
    schedule and runs a background thread to monitor pending order status.

    Portfolio state is held in memory and updated after every trade.
    All shared state is protected by dedicated locks.
    """

    def __init__(self):
        self._state_lock = threading.Lock()
        self._portfolio_state: dict = {
            "equity":          float(os.getenv("PAPER_EQUITY", "10000")),
            "buying_power":    float(os.getenv("PAPER_EQUITY", "10000")),
            "portfolio_value": float(os.getenv("PAPER_EQUITY", "10000")),
            "trades_today":    0,
            "daily_pnl":       0.0,
            "open_positions":  [],
        }

        # Pending orders tracked by the background monitor
        # { order_id: {"ticker": str, "action": str, "quantity": int, "status": str} }
        self._pending_orders_lock = threading.Lock()
        self._pending_orders: dict[str, dict] = {}

        self._apscheduler = None   # set in start()
        logger.info("[TradingScheduler] Initialised — portfolio_value=$%.2f",
                    self._portfolio_state["equity"])

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_now(self) -> None:
        """Trigger one immediate analysis cycle (for --run-now / testing)."""
        logger.info("[TradingScheduler] run_now() called")
        self.run_analysis_cycle(label="manual", min_confidence=_STANDARD_CONFIDENCE)

    def start(self) -> None:
        """
        Start the blocking APScheduler and the background order monitor.
        Blocks until SIGTERM / SIGINT.

        Jobs (SGT, Mon-Fri):
            19:00  reset_daily_state()
            20:00  run_analysis_cycle("pre_market",  min_confidence=8.5)
            21:30  run_analysis_cycle("ny_open",     min_confidence=7.0)
            03:00  run_analysis_cycle("afternoon",   min_confidence=7.0)
            04:00  market_close_cycle()
        """
        self._apscheduler = BlockingScheduler(timezone=_TZ_SCHED)

        self._apscheduler.add_job(
            func=self.universe_update_cycle,
            trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0,
                                timezone=_TZ_SCHED),
            id="universe_update",
            name="Universe auto-refresh (18:00 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=self.reset_daily_state,
            trigger=CronTrigger(day_of_week="mon-fri", hour=19, minute=0,
                                timezone=_TZ_SCHED),
            id="reset_daily",
            name="Reset daily counters (19:00 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=self.run_daily_screen,
            trigger=CronTrigger(day_of_week="mon-fri", hour=19, minute=0,
                                timezone=_TZ_SCHED),
            id="daily_screen",
            name="Momentum screener — top-15 universe (19:00 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=lambda: self.run_analysis_cycle(
                "pre_market", min_confidence=_PREMARKET_CONFIDENCE
            ),
            trigger=CronTrigger(day_of_week="mon-fri", hour=20, minute=0,
                                timezone=_TZ_SCHED),
            id="analysis_pre_market",
            name="Pre-market scan (20:00 SGT — min confidence 8.5)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=lambda: self.run_analysis_cycle(
                "ny_open", min_confidence=_STANDARD_CONFIDENCE
            ),
            trigger=CronTrigger(day_of_week="mon-fri", hour=21, minute=30,
                                timezone=_TZ_SCHED),
            id="analysis_ny_open",
            name="NYSE open analysis (21:30 SGT — min confidence 7.0)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=lambda: self.run_analysis_cycle(
                "afternoon", min_confidence=_STANDARD_CONFIDENCE
            ),
            trigger=CronTrigger(day_of_week="mon-fri", hour=3, minute=0,
                                timezone=_TZ_SCHED),
            id="analysis_afternoon",
            name="Afternoon momentum (03:00 SGT — min confidence 7.0)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=self.market_close_cycle,
            trigger=CronTrigger(day_of_week="mon-sat", hour=4, minute=0,
                                timezone=_TZ_SCHED),
            id="market_close",
            name="Market close P&L summary (04:00 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=self.run_weekly_learning_summary,
            trigger=CronTrigger(day_of_week="sun", hour=9, minute=0,
                                timezone=_TZ_SCHED),
            id="weekly_learning_summary",
            name="Weekly learning summary (09:00 SGT Sunday)",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # Start background order-status monitor thread
        monitor_thread = threading.Thread(
            target=self._order_monitor_loop,
            name="order-monitor",
            daemon=True,
        )
        monitor_thread.start()
        logger.info("[TradingScheduler] Order monitor started (interval=%ds)",
                    _ORDER_MONITOR_INTERVAL)

        # Start Telegram command listener thread
        try:
            from monitoring.telegram_listener import TelegramCommandListener
            listener = TelegramCommandListener(
                screen_callback=self.run_daily_screen,
                universe_callback=self.universe_update_cycle,
            )
            listener.listen_in_background()
        except Exception as exc:
            logger.warning("[TradingScheduler] Telegram listener not started: %s", exc)

        def _shutdown(signum, frame):
            logger.info("[TradingScheduler] Shutdown signal — stopping")
            self._apscheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT,  _shutdown)

        logger.info("[TradingScheduler] Starting (timezone: %s)", _TZ_SCHED)
        for job in self._apscheduler.get_jobs():
            logger.info("  • %s", job.name)

        try:
            self._apscheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("[TradingScheduler] Stopped")

    def reset_daily_state(self) -> None:
        """Zero trades_today and daily_pnl. Called at 19:00 SGT each weekday."""
        with self._state_lock:
            self._portfolio_state["trades_today"] = 0
            self._portfolio_state["daily_pnl"]    = 0.0
        logger.info(
            "[TradingScheduler] Daily state reset at %s",
            datetime.now(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT"),
        )

    def run_analysis_cycle(
        self,
        label: str = "scheduled",
        tickers_override: list | None = None,
        min_confidence: float = _STANDARD_CONFIDENCE,
    ) -> None:
        """
        Full watchlist analysis cycle.

        Args:
            label:            Descriptive label ("pre_market", "ny_open", …).
                              "pre_market" triggers higher confidence bar and
                              per-ticker scan telegrams.
            tickers_override: When set, analyse only these tickers.
            min_confidence:   Minimum confidence score to place a trade this cycle.
        """
        started_at = datetime.now(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
        logger.info("=" * 60)
        logger.info("[TradingScheduler] Cycle START  label=%s  min_conf=%.1f  time=%s",
                    label, min_confidence, started_at)
        logger.info("=" * 60)

        executor = self._make_executor()
        self._refresh_portfolio(executor)

        tickers = tickers_override if tickers_override is not None else self._load_watchlist()
        if not tickers:
            logger.error("[TradingScheduler] Empty watchlist — aborting cycle")
            return

        logger.info("[TradingScheduler] Watchlist (%d): %s", len(tickers), tickers)

        try:
            from monitoring.telegram_alerts import send_cycle_started
            send_cycle_started(label, tickers)
        except Exception as exc:
            logger.warning("[TradingScheduler] Telegram cycle-start alert: %s", exc)

        approved_count = [0]
        blocked_count  = [0]

        for ticker in tickers:
            try:
                self._run_ticker_pipeline(
                    ticker, executor, approved_count, blocked_count,
                    label=label, min_confidence=min_confidence,
                )
            except Exception as exc:
                logger.error(
                    "[TradingScheduler] Unhandled error for %s: %s",
                    ticker, exc, exc_info=True,
                )
                blocked_count[0] += 1

        try:
            from monitoring.telegram_alerts import send_cycle_complete
            send_cycle_complete(label, approved_count[0], blocked_count[0])
        except Exception as exc:
            logger.warning("[TradingScheduler] Telegram cycle-end alert: %s", exc)

        logger.info("=" * 60)
        logger.info(
            "[TradingScheduler] Cycle DONE  label=%s  approved=%d  blocked=%d",
            label, approved_count[0], blocked_count[0],
        )
        logger.info("=" * 60)

    def universe_update_cycle(self) -> None:
        """
        18:00 SGT — rebuild watchlist_universe.txt from live market data.
        Sends a Telegram diff notification when done.
        Also triggered by /universe refresh Telegram command.
        """
        from data.universe_builder import UniverseBuilder
        from monitoring.telegram_alerts import send_universe_updated

        logger.info("[TradingScheduler] Universe update starting")
        try:
            result = UniverseBuilder().build()
            send_universe_updated(result)
            logger.info(
                "[TradingScheduler] Universe updated — %d tickers  added=%d  removed=%d",
                result["total"], len(result["added"]), len(result["removed"]),
            )
        except Exception as exc:
            logger.error(
                "[TradingScheduler] Universe update failed: %s", exc, exc_info=True
            )

    def run_daily_screen(self) -> None:
        """
        19:00 SGT — screen universe, score by momentum, send top-15 via Telegram.
        Called by the scheduler and also by the /screen Telegram command.
        """
        from data.screener import MomentumScreener
        from monitoring.telegram_alerts import send_screener_results

        logger.info("[TradingScheduler] Daily screen starting")
        try:
            results = MomentumScreener().screen()
            if results:
                send_screener_results(results)
                logger.info(
                    "[TradingScheduler] Screen complete — top: %s (%.1f)",
                    results[0]["ticker"], results[0]["score"],
                )
            else:
                logger.warning("[TradingScheduler] Screener returned no results")
        except Exception as exc:
            logger.error("[TradingScheduler] Daily screen failed: %s", exc, exc_info=True)

    def market_close_cycle(self) -> None:
        """
        04:00 SGT — fetch open positions, send P&L summary. No new trades.
        """
        started_at = datetime.now(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
        logger.info("[TradingScheduler] Market close cycle — %s", started_at)

        executor = self._make_executor()
        self._refresh_portfolio(executor)

        with self._state_lock:
            state   = self._portfolio_state.copy()
            positions = list(state.get("open_positions", []))

        logger.info(
            "[TradingScheduler] Close summary — equity=$%.2f  daily_pnl=$%.2f  "
            "positions=%d",
            state["equity"], state["daily_pnl"], len(positions),
        )

        try:
            from monitoring.telegram_alerts import send_market_close_summary
            send_market_close_summary(state, positions)
        except Exception as exc:
            logger.warning("[TradingScheduler] Telegram close-summary alert: %s", exc)

    def run_weekly_learning_summary(self) -> None:
        """
        Sunday 09:00 SGT — generate a 7-day reflection summary and send via Telegram.
        """
        logger.info("[TradingScheduler] Weekly learning summary starting")
        try:
            from agents.memory.reflection_engine import ReflectionEngine
            summary = ReflectionEngine().generate_weekly_summary()
        except Exception as exc:
            logger.error("[TradingScheduler] ReflectionEngine weekly summary failed: %s", exc)
            return

        total    = summary.get("total_trades", 0)
        wins     = summary.get("wins", 0)
        losses   = summary.get("losses", 0)
        win_rate = summary.get("win_rate", 0.0)

        if total == 0:
            logger.info("[TradingScheduler] No closed trades in last 7 days — skipping Telegram")
            return

        best  = summary.get("best_trade", {})
        worst = summary.get("worst_trade", {})
        best_str  = (
            f"{best.get('ticker','?')} ${float(best.get('pnl', 0)):+.2f}"
            if best else "N/A"
        )
        worst_str = (
            f"{worst.get('ticker','?')} ${float(worst.get('pnl', 0)):+.2f}"
            if worst else "N/A"
        )

        patterns = summary.get("top_patterns", {})
        pattern_str = ", ".join(
            f"{tag}×{count}" for tag, count in list(patterns.items())[:3]
        ) or "none"

        warning = summary.get("warning_pattern", "")
        new_lessons = summary.get("new_lessons", [])

        lines = [
            "📚 <b>Weekly Learning Summary</b>",
            f"Period: last 7 days\n",
            f"Trades: {total}  |  Wins: {wins}  |  Losses: {losses}",
            f"Win rate: {win_rate}%",
            f"Best trade:  {best_str}",
            f"Worst trade: {worst_str}",
        ]
        if pattern_str != "none":
            lines.append(f"Top patterns: {pattern_str}")
        if warning:
            lines.append(f"\n⚠️ Warning pattern in losses: {warning}")
        if new_lessons:
            lines.append("\n💡 <b>New lessons this week:</b>")
            for lesson in new_lessons[:5]:
                lines.append(f"  • {lesson}")

        try:
            from monitoring.telegram_alerts import TelegramAlerter
            TelegramAlerter().send_text("\n".join(lines))
        except Exception as exc:
            logger.warning("[TradingScheduler] Telegram weekly summary alert failed: %s", exc)

        logger.info(
            "[TradingScheduler] Weekly summary sent — %d trades, %.1f%% win rate",
            total, win_rate,
        )

    # ------------------------------------------------------------------
    # Private helpers — pipeline
    # ------------------------------------------------------------------

    def _us_tickers(self) -> list:
        """Return only US-listed tickers (no dot suffix, e.g. AAPL not D05.SI)."""
        return [t for t in self._load_watchlist() if "." not in t]

    def _load_watchlist(self) -> list:
        from monitoring.watchlist import WatchlistManager
        tickers = WatchlistManager().get_tickers()
        if not tickers:
            logger.error("[TradingScheduler] Watchlist is empty — check watchlist.txt")
        return tickers

    def _make_executor(self):
        """
        Construct the configured broker connector.
        BROKER env var selects 'moomoo' (default) or 'alpaca'.
        Returns None if the broker cannot be initialised.
        """
        broker = os.getenv("BROKER", "moomoo").lower()

        if broker == "moomoo":
            try:
                from execution.moomoo import MooMooConnector
                return MooMooConnector()
            except Exception as exc:
                logger.error("[TradingScheduler] MooMooConnector init failed: %s", exc)
                return None
        else:
            api_key    = os.getenv("ALPACA_API_KEY", "")
            secret_key = os.getenv("ALPACA_SECRET_KEY", "")
            if api_key in ("", "your-key-here") or secret_key in ("", "your-key-here"):
                logger.warning(
                    "[TradingScheduler] Alpaca keys not configured — "
                    "order execution disabled"
                )
                return None
            try:
                from execution.alpaca import AlpacaExecutor
                return AlpacaExecutor()
            except Exception as exc:
                logger.error("[TradingScheduler] AlpacaExecutor init failed: %s", exc)
                return None

    def _refresh_portfolio(self, executor) -> None:
        """Pull live account state and positions from the active broker."""
        if executor is None:
            return
        try:
            if hasattr(executor, "get_account_balance"):
                bal = executor.get_account_balance()
                if bal:
                    us = bal.get("by_market", {}).get("US", {})
                    pv = us.get("total_assets", 0.0)
                    with self._state_lock:
                        self._portfolio_state["equity"]          = pv
                        self._portfolio_state["buying_power"]    = us.get("cash", 0.0)
                        self._portfolio_state["portfolio_value"] = pv
            else:
                account = executor.get_account()
                if account:
                    with self._state_lock:
                        self._portfolio_state["equity"]          = account["equity"]
                        self._portfolio_state["buying_power"]    = account["buying_power"]
                        self._portfolio_state["portfolio_value"] = account["portfolio_value"]
                        self._portfolio_state["daily_pnl"]       = account["daily_pnl"]

            with self._state_lock:
                self._portfolio_state["open_positions"] = executor.get_positions()

            logger.info(
                "[TradingScheduler] Portfolio — equity=$%.2f  daily_pnl=$%.2f  "
                "positions=%d  trades_today=%d",
                self._portfolio_state["equity"],
                self._portfolio_state["daily_pnl"],
                len(self._portfolio_state["open_positions"]),
                self._portfolio_state["trades_today"],
            )
        except Exception as exc:
            logger.warning("[TradingScheduler] Portfolio refresh failed: %s", exc)

    def _run_ticker_pipeline(
        self,
        ticker: str,
        executor,
        approved_count: list,
        blocked_count: list,
        label: str = "scheduled",
        min_confidence: float = _STANDARD_CONFIDENCE,
    ) -> None:
        """
        Full pipeline for one ticker: fetch → analyse → gate → size → execute.

        For label=="pre_market":
            - Sends per-ticker scan signal to Telegram before trading decision
            - Enforces min_confidence=8.5 (vs 7.0 standard)
            - Sends "⚡ High confidence pre-market trade placed" if order goes through
        """
        from data.fetcher import DataFetcher
        from agents.trading_agents import TradingAgentsWrapper
        from risk.risk_gate import RiskGate
        from risk.position_sizer import PositionSizer, PositionSizerError
        from monitoring.logger import log_decision, log_trade
        from monitoring.telegram_alerts import (
            send_trade_placed, send_trade_blocked,
            send_order_filled, send_order_pending,
            send_premarket_scan, send_premarket_trade_placed,
        )

        trading_mode = os.getenv("TRADING_MODE", "paper").upper()
        is_premarket = (label == "pre_market")
        logger.info("[%s] ── Pipeline start  label=%s  min_conf=%.1f ──────────",
                    ticker, label, min_confidence)

        # 1. Fetch market data
        try:
            data = DataFetcher().fetch(ticker)
        except Exception as exc:
            logger.error("[%s] DataFetcher failed: %s", ticker, exc, exc_info=True)
            return

        # 2. Run 7-agent pipeline
        try:
            decision = TradingAgentsWrapper().analyse(data)
        except Exception as exc:
            logger.error("[%s] Agent pipeline failed: %s", ticker, exc, exc_info=True)
            return

        try:
            log_decision(ticker, decision, data)
        except Exception as exc:
            logger.warning("[%s] log_decision failed: %s", ticker, exc)

        logger.info(
            "[%s] Decision: %s  confidence=%.1f  flags=%s",
            ticker, decision["action"], decision["confidence"],
            decision.get("risk_flags", []),
        )

        # 2a. Pre-market per-ticker scan signal (sent regardless of whether trade is placed)
        if is_premarket:
            try:
                send_premarket_scan(ticker, decision["action"], decision["confidence"])
            except Exception as exc:
                logger.warning("[%s] Telegram pre-market scan alert failed: %s", ticker, exc)

        # 3a. Cycle-specific confidence pre-check (before RiskGate)
        if decision["confidence"] < min_confidence:
            logger.info(
                "[%s] Confidence %.1f below cycle minimum %.1f — no trade placed",
                ticker, decision["confidence"], min_confidence,
            )
            blocked_count[0] += 1
            return

        # 3b. Risk Gate
        with self._state_lock:
            state_snapshot = self._portfolio_state.copy()

        gate_result = RiskGate().check(decision, state_snapshot)

        if not gate_result["approved"]:
            reason = gate_result["reason"]
            logger.info("[%s] BLOCKED: %s", ticker, reason)
            blocked_count[0] += 1
            try:
                send_trade_blocked(
                    ticker, decision["action"], reason, decision["confidence"]
                )
            except Exception as exc:
                logger.warning("[%s] Telegram blocked alert failed: %s", ticker, exc)
            return

        # 4. Position sizer
        entry_price_raw = (data.get("quote") or {}).get("price")
        if entry_price_raw is None:
            logger.warning("[%s] No quote price — skipping order", ticker)
            blocked_count[0] += 1
            return

        try:
            entry_price = float(entry_price_raw)
            with self._state_lock:
                portfolio_value = self._portfolio_state["equity"]

            sizing = PositionSizer().calculate(
                ticker=ticker,
                action=decision["action"],
                entry_price=entry_price,
                portfolio_value=portfolio_value,
                confidence=decision["confidence"],
                market_data=data,
            )
        except PositionSizerError as exc:
            logger.warning("[%s] PositionSizer rejected: %s", ticker, exc)
            blocked_count[0] += 1
            return
        except Exception as exc:
            logger.error("[%s] PositionSizer error: %s", ticker, exc, exc_info=True)
            blocked_count[0] += 1
            return

        logger.info(
            "[%s] Sized: qty=%d  value=$%.2f  SL=$%.2f  TP=$%.2f",
            ticker, sizing["quantity"], sizing["position_value"],
            sizing["stop_loss_price"], sizing["take_profit_price"],
        )

        # 5. Place main order
        if executor is None:
            logger.warning(
                "[%s] No broker — would place %s × %d (paper sim)",
                ticker, decision["action"], sizing["quantity"],
            )
            approved_count[0] += 1
            return

        order = executor.place_order(
            ticker=ticker,
            action=decision["action"],
            quantity=sizing["quantity"],
        )
        if order is None:
            logger.error("[%s] Order submission failed", ticker)
            blocked_count[0] += 1
            return

        approved_count[0] += 1
        oid = order.get("order_id") or order.get("id", "")
        logger.info("[%s] Order placed — id=%s  status=%s",
                    ticker, oid, order.get("status"))

        # 5a. "Order Placed" Telegram — immediate confirmation
        try:
            send_trade_placed(
                ticker=ticker,
                action=decision["action"],
                sizing=sizing,
                order=order,
                confidence=decision["confidence"],
                mode=trading_mode,
            )
        except Exception as exc:
            logger.warning("[%s] Telegram order-placed alert failed: %s", ticker, exc)

        # 5b. Pre-market high-confidence trade alert
        if is_premarket:
            try:
                send_premarket_trade_placed(
                    ticker, decision["action"], sizing["quantity"], oid
                )
            except Exception as exc:
                logger.warning("[%s] Telegram pre-market trade alert failed: %s", ticker, exc)

        # 5c. Bracket orders for BUY (SL + TP limit sells)
        if decision["action"] == "BUY":
            sl_price = sizing["stop_loss_price"]
            tp_price = sizing["take_profit_price"]
            qty      = sizing["quantity"]

            sl_oid = ""
            try:
                sl_order = executor.place_order(
                    ticker=ticker, action="SELL", quantity=qty,
                    order_type="limit", price=sl_price,
                )
                if sl_order:
                    sl_oid = sl_order.get("order_id", "")
                    logger.info("[%s] SL order placed — id=%s @ $%.2f",
                                ticker, sl_oid, sl_price)
                else:
                    logger.warning("[%s] SL order submission failed", ticker)
            except Exception as exc:
                logger.warning("[%s] SL order error: %s", ticker, exc)

            tp_oid = ""
            try:
                tp_order = executor.place_order(
                    ticker=ticker, action="SELL", quantity=qty,
                    order_type="limit", price=tp_price,
                )
                if tp_order:
                    tp_oid = tp_order.get("order_id", "")
                    logger.info("[%s] TP order placed — id=%s @ $%.2f",
                                ticker, tp_oid, tp_price)
                else:
                    logger.warning("[%s] TP order submission failed", ticker)
            except Exception as exc:
                logger.warning("[%s] TP order error: %s", ticker, exc)

            # Register bracket orders with the background monitor immediately
            if sl_oid:
                self._register_pending_order(sl_oid, ticker, "SELL", qty, "")
            if tp_oid:
                self._register_pending_order(tp_oid, ticker, "SELL", qty, "")

        # 6. Log trade
        try:
            log_trade(
                ticker=ticker,
                action=decision["action"],
                quantity=sizing["quantity"],
                price=entry_price,
                order_id=oid,
                stop_loss=sizing["stop_loss_price"],
                take_profit=sizing["take_profit_price"],
            )
        except Exception as exc:
            logger.warning("[%s] log_trade failed: %s", ticker, exc)

        # 7. Update portfolio state
        with self._state_lock:
            self._portfolio_state["trades_today"] += 1

        self._refresh_portfolio(executor)

        # 8. Poll for fill (60 s); register with monitor if still pending
        filled = self._poll_order_fill(
            executor=executor,
            order_id=oid,
            ticker=ticker,
            action=decision["action"],
            quantity=sizing["quantity"],
            send_filled=send_order_filled,
            send_pending=send_order_pending,
        )
        if not filled and oid:
            self._register_pending_order(
                oid, ticker, decision["action"], sizing["quantity"],
                order.get("status", ""),
            )

        logger.info("[%s] ── Pipeline complete ─────────────────────────", ticker)

    def _poll_order_fill(
        self,
        executor,
        order_id: str,
        ticker: str,
        action: str,
        quantity: int,
        send_filled,
        send_pending,
        max_wait: int = 60,
        interval: int = 5,
    ) -> bool:
        """
        Poll broker for fill status every `interval` s for up to `max_wait` s.

        Returns True if order filled within the window, False otherwise.
        Sends "Order Filled" or "Order Pending" Telegram accordingly.
        """
        if not order_id:
            return False

        if executor is None or not hasattr(executor, "get_order_status"):
            logger.info("[%s] Broker does not support status polling — pending alert", ticker)
            try:
                send_pending(ticker, action, quantity, order_id)
            except Exception as exc:
                logger.warning("[%s] Telegram pending alert failed: %s", ticker, exc)
            return False

        logger.info(
            "[%s] Polling for fill (id=%s, max=%ds, interval=%ds)",
            ticker, order_id, max_wait, interval,
        )
        deadline = time.time() + max_wait

        while time.time() < deadline:
            time.sleep(interval)
            try:
                info = executor.get_order_status(order_id, ticker)
            except Exception as exc:
                logger.warning("[%s] get_order_status error: %s", ticker, exc)
                continue

            status = info.get("status", "")
            logger.info("[%s] Order %s status: %s", ticker, order_id, status)

            if "FILLED_ALL" in status:
                fill_price = info.get("fill_price", 0.0)
                logger.info("[%s] Filled — price=$%.4f", ticker, fill_price)
                try:
                    send_filled(ticker, action, quantity, order_id, fill_price)
                except Exception as exc:
                    logger.warning("[%s] Telegram filled alert failed: %s", ticker, exc)
                return True

            if any(s in status for s in (
                "CANCELLED", "FAILED", "SUBMIT_FAILED", "DELETED", "TIMEOUT"
            )):
                logger.warning("[%s] Order %s terminal status: %s", ticker, order_id, status)
                return False

        logger.info("[%s] Not filled within %ds — pending alert", ticker, max_wait)
        try:
            send_pending(ticker, action, quantity, order_id)
        except Exception as exc:
            logger.warning("[%s] Telegram pending alert failed: %s", ticker, exc)
        return False

    # ------------------------------------------------------------------
    # Private helpers — order monitor
    # ------------------------------------------------------------------

    def _register_pending_order(
        self,
        order_id: str,
        ticker: str,
        action: str,
        quantity: int,
        status: str = "",
    ) -> None:
        """Add an order to the background monitor's watch list."""
        if not order_id:
            return
        with self._pending_orders_lock:
            self._pending_orders[order_id] = {
                "ticker":   ticker,
                "action":   action,
                "quantity": quantity,
                "status":   status,
            }
        logger.debug(
            "[OrderMonitor] Registered %s  %s %s × %d",
            order_id, action, ticker, quantity,
        )

    def _order_monitor_loop(self) -> None:
        """
        Background daemon thread body.
        Checks all pending orders every _ORDER_MONITOR_INTERVAL seconds.
        Creates one persistent MooMooConnector and recreates it on failure.
        """
        logger.info("[OrderMonitor] Loop started")
        executor = None

        while True:
            time.sleep(_ORDER_MONITOR_INTERVAL)
            try:
                # Lazy-create or recreate after failure
                if executor is None:
                    executor = self._make_executor()
                if executor is None or not hasattr(executor, "get_order_status"):
                    continue

                with self._pending_orders_lock:
                    if not self._pending_orders:
                        continue

                self._check_pending_orders(executor)

            except Exception as exc:
                logger.error("[OrderMonitor] Unhandled error: %s", exc, exc_info=True)
                try:
                    executor.close()
                except Exception:
                    pass
                executor = None

    def _check_pending_orders(self, executor) -> None:
        """
        Poll every order in _pending_orders for status changes.
        On FILLED / CANCELLED / REJECTED: send Telegram, log to trades.csv,
        remove from the watch list.
        """
        from monitoring.telegram_alerts import (
            send_order_filled, send_order_cancelled, send_order_rejected,
        )
        from monitoring.logger import log_trade

        with self._pending_orders_lock:
            snapshot = dict(self._pending_orders)

        if not snapshot:
            return

        logger.debug("[OrderMonitor] Checking %d pending order(s)", len(snapshot))
        to_remove: list[str] = []

        for order_id, info in snapshot.items():
            ticker      = info["ticker"]
            action      = info["action"]
            quantity    = info["quantity"]
            prev_status = info["status"]

            try:
                result = executor.get_order_status(order_id, ticker)
            except Exception as exc:
                logger.warning(
                    "[OrderMonitor] get_order_status failed (%s): %s", order_id, exc
                )
                continue

            new_status = result.get("status", "")
            if not new_status or new_status == prev_status:
                continue

            logger.info(
                "[OrderMonitor] %s %s: %s → %s",
                ticker, order_id, prev_status or "?", new_status,
            )

            if "FILLED_ALL" in new_status:
                fill_price = result.get("fill_price", 0.0)
                try:
                    send_order_filled(ticker, action, quantity, order_id, fill_price)
                except Exception as exc:
                    logger.warning("[OrderMonitor] Telegram filled failed: %s", exc)
                try:
                    log_trade(ticker, "FILLED", quantity, fill_price, order_id)
                except Exception as exc:
                    logger.warning("[OrderMonitor] log_trade FILLED failed: %s", exc)
                # Trigger reflection after a SELL fill so lessons are captured
                if action.upper() == "SELL":
                    def _reflect(t: str = ticker) -> None:
                        try:
                            from agents.memory.reflection_engine import ReflectionEngine
                            count = ReflectionEngine().run_pending_reflections()
                            logger.info(
                                "[OrderMonitor] Post-fill reflection complete — %d new reflection(s) for %s",
                                count, t,
                            )
                        except Exception as exc:
                            logger.warning("[OrderMonitor] Post-fill reflection failed for %s: %s", t, exc)
                    threading.Thread(target=_reflect, name=f"reflect-{ticker}", daemon=True).start()
                to_remove.append(order_id)

            elif "CANCELLED" in new_status:
                try:
                    send_order_cancelled(ticker, action, quantity, order_id)
                except Exception as exc:
                    logger.warning("[OrderMonitor] Telegram cancelled failed: %s", exc)
                try:
                    log_trade(ticker, "CANCELLED", quantity, 0.0, order_id)
                except Exception as exc:
                    logger.warning("[OrderMonitor] log_trade CANCELLED failed: %s", exc)
                to_remove.append(order_id)

            elif any(s in new_status for s in (
                "FAILED", "SUBMIT_FAILED", "REJECTED", "DELETED", "TIMEOUT"
            )):
                try:
                    send_order_rejected(ticker, action, quantity, order_id, new_status)
                except Exception as exc:
                    logger.warning("[OrderMonitor] Telegram rejected failed: %s", exc)
                try:
                    log_trade(ticker, "REJECTED", quantity, 0.0, order_id)
                except Exception as exc:
                    logger.warning("[OrderMonitor] log_trade REJECTED failed: %s", exc)
                to_remove.append(order_id)

            else:
                # Intermediate state — update stored status, keep monitoring
                with self._pending_orders_lock:
                    if order_id in self._pending_orders:
                        self._pending_orders[order_id]["status"] = new_status

        if to_remove:
            with self._pending_orders_lock:
                for oid in to_remove:
                    self._pending_orders.pop(oid, None)
            logger.debug("[OrderMonitor] Removed %d completed order(s)", len(to_remove))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Trading Bot scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scheduler.py             # start the scheduler (runs until killed)
  python scheduler.py --run-now   # one immediate cycle then exit
        """,
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Trigger one analysis cycle immediately and exit",
    )
    args = parser.parse_args()

    ts = TradingScheduler()

    if args.run_now:
        ts.run_now()
        sys.exit(0)

    ts.start()


if __name__ == "__main__":
    main()
