"""
scheduler.py — APScheduler-driven pipeline runner for the AI trading bot.

Schedule (SGT / Asia/Singapore):
    08:50  Mon-Fri   reset_daily_state()              — clear counters before open
    09:00  Mon-Fri   run_analysis_cycle("open")       — SGX market-open analysis
    15:00  Mon-Fri   run_analysis_cycle("close")      — SGX pre-close analysis
    21:30  Mon-Fri   run_analysis_cycle("ny_open")    — NYSE open, US tickers only

Usage:
    python scheduler.py              # start the scheduler (blocking)
    python scheduler.py --run-now    # one immediate cycle then exit

Import:
    from scheduler import TradingScheduler
    ts = TradingScheduler()
    ts.run_now()          # one cycle
    ts.start()            # blocking scheduler
"""

import argparse
import logging
import os
import signal
import sys
import threading
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

_SGT      = ZoneInfo("Asia/Singapore")
_TZ_SCHED = "Asia/Singapore"


# ---------------------------------------------------------------------------
# TradingScheduler
# ---------------------------------------------------------------------------

class TradingScheduler:
    """
    Orchestrates the full analysis → risk → execution pipeline on a cron
    schedule, and exposes run_now() for manual one-shot cycles.

    Portfolio state is held in memory and updated after every trade.
    All methods are thread-safe via an internal lock.
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
        self._apscheduler = None   # set in start()
        logger.info("[TradingScheduler] Initialised — portfolio_value=$%.2f",
                    self._portfolio_state["equity"])

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_now(self) -> None:
        """Trigger one immediate analysis cycle (for --run-now / testing)."""
        logger.info("[TradingScheduler] run_now() called")
        self.run_analysis_cycle(label="manual")

    def start(self) -> None:
        """
        Start the blocking APScheduler.  Blocks until SIGTERM / SIGINT.

        Jobs registered:
            08:50 SGT Mon-Fri  reset_daily_state()
            09:00 SGT Mon-Fri  run_analysis_cycle("open")      — full watchlist
            15:00 SGT Mon-Fri  run_analysis_cycle("close")     — full watchlist
            21:30 SGT Mon-Fri  run_analysis_cycle("ny_open")   — US tickers only
        """
        self._apscheduler = BlockingScheduler(timezone=_TZ_SCHED)

        self._apscheduler.add_job(
            func=self.reset_daily_state,
            trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=50,
                                timezone=_TZ_SCHED),
            id="reset_daily",
            name="Reset daily counters (08:50 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=lambda: self.run_analysis_cycle("open"),
            trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=0,
                                timezone=_TZ_SCHED),
            id="analysis_open",
            name="SGX market-open analysis (09:00 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=lambda: self.run_analysis_cycle("close"),
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=0,
                                timezone=_TZ_SCHED),
            id="analysis_close",
            name="SGX pre-close analysis (15:00 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._apscheduler.add_job(
            func=lambda: self.run_analysis_cycle(
                "ny_open",
                tickers_override=self._us_tickers(),
            ),
            trigger=CronTrigger(day_of_week="mon-fri", hour=21, minute=30,
                                timezone=_TZ_SCHED),
            id="analysis_ny_open",
            name="NYSE market-open analysis (21:30 SGT)",
            replace_existing=True,
            misfire_grace_time=300,
        )

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
        """Zero trades_today and daily_pnl. Called at 08:50 SGT each weekday."""
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
    ) -> None:
        """
        Full watchlist analysis cycle.

        Args:
            label:            Descriptive label logged with each cycle
                              ("open", "close", "ny_open", "manual", …).
            tickers_override: When provided, analyse only these tickers instead
                              of the full watchlist.  Used by the NYSE-open job
                              to restrict the cycle to US-listed symbols.

        Steps:
            1. Connect to broker (order execution disabled if keys not configured)
            2. Refresh portfolio state
            3. Run per-ticker pipeline for every ticker in the watchlist
            4. Send Telegram cycle summary
        """
        started_at = datetime.now(_SGT).strftime("%Y-%m-%d %H:%M:%S SGT")
        logger.info("=" * 60)
        logger.info("[TradingScheduler] Cycle START  label=%s  time=%s",
                    label, started_at)
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
                    ticker, executor, approved_count, blocked_count
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

    # ------------------------------------------------------------------
    # Private helpers
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
        Construct an AlpacaExecutor.  Returns None if keys are placeholder
        values — callers treat None as 'no broker available'.
        """
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
        """Pull live equity / buying_power / positions from Alpaca into state."""
        if executor is None:
            return
        try:
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
    ) -> None:
        """
        Full pipeline for one ticker: fetch → analyse → gate → size → execute.

        approved_count / blocked_count are single-element lists used as
        mutable counters across the watchlist loop.
        """
        from data.fetcher import DataFetcher
        from agents.trading_agents import TradingAgentsWrapper
        from risk.risk_gate import RiskGate
        from risk.position_sizer import PositionSizer, PositionSizerError
        from monitoring.logger import log_decision, log_trade
        from monitoring.telegram_alerts import send_trade_placed, send_trade_blocked

        trading_mode = os.getenv("TRADING_MODE", "paper").upper()
        logger.info("[%s] ── Pipeline start ──────────────────────────", ticker)

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

        # 3. Risk Gate
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

        # 5. Place order
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
        logger.info("[%s] Order placed — id=%s  status=%s",
                    ticker, order.get("id"), order.get("status"))

        # 6. Log trade
        try:
            log_trade(
                ticker=ticker,
                action=decision["action"],
                quantity=sizing["quantity"],
                price=entry_price,
                order_id=order.get("id", ""),
            )
        except Exception as exc:
            logger.warning("[%s] log_trade failed: %s", ticker, exc)

        # 7. Update portfolio state
        with self._state_lock:
            self._portfolio_state["trades_today"] += 1

        self._refresh_portfolio(executor)

        # 8. Telegram alert
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
            logger.warning("[%s] Telegram trade alert failed: %s", ticker, exc)

        logger.info("[%s] ── Pipeline complete ─────────────────────────", ticker)


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
