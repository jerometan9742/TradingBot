"""
TelegramCommandListener — long-polls Telegram getUpdates and handles all
bot commands including multi-step trade flows.

──────────────────────────────
INFO COMMANDS
──────────────────────────────
/help               — all commands grouped by category
/watchlist          — active watchlist.txt contents
/universe [...]     — universe pool commands (list/add/remove/refresh)
/signals            — latest confidence scores for watchlist tickers
/news TICKER        — latest 5 news headlines + sentiment (Finnhub)
/chart TICKER       — technical summary (price, RSI, EMA, MACD)
/summary            — full account snapshot
/history            — last 10 closed trades from logs/trades.csv
/status             — VPS service status check
/orders             — open orders via MooMoo
/positions          — open positions with P&L
/pnl                — today + unrealised P&L summary
/cash               — available cash balance

──────────────────────────────
SCREENING COMMANDS
──────────────────────────────
/screen             — run momentum screener now
/screenstock TICKER — full 7-agent TradingAgents pipeline on one ticker

──────────────────────────────
WATCHLIST COMMANDS
──────────────────────────────
/add TICKER …       — add to watchlist.txt
/remove TICKER …    — remove from watchlist.txt

──────────────────────────────
BOT CONTROL COMMANDS
──────────────────────────────
/pause              — pause trading without full kill switch (pause.lock)
/resume             — delete pause.lock, re-enable trading
/killswitch on      — write kill_switch.lock, halt all trading
/killswitch off     — delete kill_switch.lock, resume trading

──────────────────────────────
TRADE COMMANDS (multi-step, 60 s timeout)
──────────────────────────────
/buy TICKER         — 4-step BUY flow: qty → SL/TP → confirm → place
/sell TICKER        — 3-step SELL flow: qty → confirm → place
/cancel             — list open orders → pick one → cancel it

Security: only TELEGRAM_CHAT_ID is authorised.
Kill switch + pause: all trade commands abort if kill_switch.lock or pause.lock exists.
"""

import csv
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_WATCHLIST_PATH  = Path(__file__).resolve().parent.parent / "watchlist.txt"
_UNIVERSE_PATH   = Path(__file__).resolve().parent.parent / "watchlist_universe.txt"
_KILL_SWITCH     = Path(__file__).resolve().parent.parent / "kill_switch.lock"
_PAUSE_LOCK      = Path(__file__).resolve().parent.parent / "pause.lock"
_LOG_DIR         = Path(__file__).resolve().parent.parent / "logs"
_TRADES_CSV      = _LOG_DIR / "trades.csv"
_POLL_TIMEOUT    = 30    # long-poll window (seconds)
_REQUEST_TIMEOUT = 35    # requests timeout > _POLL_TIMEOUT
_RETRY_WAIT      = 10    # back-off after poll error
_CONV_TIMEOUT    = 60    # conversation step timeout (seconds)
_ATR_SL_MULT     = 1.5   # SL = price − ATR × 1.5
_ATR_TP_MULT     = 3.0   # TP = price + ATR × 3.0
_SGT             = ZoneInfo("Asia/Singapore")

# Scheduled run times (SGT hour, minute) Mon-Fri — used by /summary and /status
_SCHEDULE_TIMES = [(19, 0), (20, 0), (21, 30), (3, 0), (4, 0)]


# ── Display helpers ────────────────────────────────────────────────────────────

def _display_ticker(futu_code: str) -> str:
    """US.AAPL → AAPL, HK.D05 → D05.SI, HK.00700 → 0700.HK"""
    if futu_code.startswith("US."):
        return futu_code[3:]
    if futu_code.startswith("HK."):
        base = futu_code[3:].lstrip("0") or "0"
        return (base + ".HK") if base.isdigit() else (base + ".SI")
    return futu_code


def _sign(value: float) -> str:
    return "+" if value >= 0 else ""


# ── Main class ────────────────────────────────────────────────────────────────

class TelegramCommandListener:
    """
    Long-polls Telegram and dispatches commands + multi-step conversation flows.

    Start with listen_in_background(). Thread is daemon so it exits with the process.
    """

    def __init__(
        self,
        screen_callback:   Optional[Callable] = None,
        universe_callback: Optional[Callable] = None,
        watchlist_path: Path = _WATCHLIST_PATH,
        universe_path:  Path = _UNIVERSE_PATH,
    ):
        self._token            = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id          = os.getenv("TELEGRAM_CHAT_ID", "")
        self._offset           = 0
        self._screen_callback  = screen_callback
        self._universe_callback = universe_callback
        self._watchlist_path   = Path(watchlist_path)
        self._universe_path    = Path(universe_path)

        # conversation state keyed by chat_id
        # {chat_id: {"flow": str, "step": str, "expires_at": float, ...data}}
        self._conversations: dict = {}

        if self._token and self._chat_id:
            logger.info("[TelegramListener] Configured — chat_id=%s", self._chat_id)
        else:
            logger.warning(
                "[TelegramListener] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set "
                "— command listener disabled"
            )

    # ── Public ────────────────────────────────────────────────────────────────

    def listen_in_background(self) -> threading.Thread:
        t = threading.Thread(target=self.start, name="telegram-listener", daemon=True)
        t.start()
        logger.info("[TelegramListener] Background thread started")
        return t

    def start(self) -> None:
        if not self._token or not self._chat_id:
            logger.warning("[TelegramListener] Not started — credentials missing")
            return
        logger.info("[TelegramListener] Polling for commands (timeout=%ds)", _POLL_TIMEOUT)
        while True:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._dispatch(update)
            except Exception as exc:
                logger.warning("[TelegramListener] Poll error: %s — retrying in %ds",
                               exc, _RETRY_WAIT)
                time.sleep(_RETRY_WAIT)

    # ── Long-poll ─────────────────────────────────────────────────────────────

    def _get_updates(self) -> list:
        resp = requests.get(
            f"https://api.telegram.org/bot{self._token}/getUpdates",
            params={
                "offset":          self._offset,
                "timeout":         _POLL_TIMEOUT,
                "allowed_updates": ["message"],
            },
            timeout=_REQUEST_TIMEOUT,
        )
        updates = resp.json().get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, update: dict) -> None:
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text    = (message.get("text") or "").strip()

        if chat_id != self._chat_id or not text:
            return

        # Expire stale conversations
        conv = self._conversations.get(chat_id)
        if conv and conv["expires_at"] < time.time():
            self._conversations.pop(chat_id, None)
            if not text.startswith("/"):
                flow   = conv.get("flow", "")
                ticker = conv.get("ticker", "")
                self._reply(
                    f"⏱ Command timed out. "
                    f"Send /{flow}{' ' + ticker if ticker else ''} to start again."
                )
                return
            conv = None

        if text.startswith("/"):
            # New command — cancel any in-progress flow silently
            self._conversations.pop(chat_id, None)

            parts = text.split()
            cmd   = parts[0].lower().split("@")[0]
            args  = parts[1:]
            logger.info("[TelegramListener] cmd=%s  args=%s  chat=%s", cmd, args, chat_id)

            if   cmd == "/help":        self._cmd_help()
            elif cmd == "/watchlist":   self._cmd_watchlist()
            elif cmd == "/universe":    self._cmd_universe(args)
            elif cmd == "/signals":     self._cmd_signals()
            elif cmd == "/news":        self._cmd_news(args)
            elif cmd == "/chart":       self._cmd_chart(args)
            elif cmd == "/summary":     self._cmd_summary()
            elif cmd == "/history":     self._cmd_history()
            elif cmd == "/stats":       self._cmd_stats()
            elif cmd == "/status":      self._cmd_status()
            elif cmd == "/orders":      self._cmd_orders()
            elif cmd == "/positions":   self._cmd_positions()
            elif cmd == "/pnl":         self._cmd_pnl()
            elif cmd == "/cash":        self._cmd_cash()
            elif cmd == "/add":         self._cmd_add(args)
            elif cmd == "/remove":      self._cmd_remove(args)
            elif cmd == "/screen":      self._cmd_screen()
            elif cmd == "/screenstock": self._cmd_screenstock(chat_id, args)
            elif cmd == "/pause":       self._cmd_pause()
            elif cmd == "/resume":      self._cmd_resume()
            elif cmd == "/killswitch":  self._cmd_killswitch(args)
            elif cmd == "/buy":         self._cmd_buy(chat_id, args)
            elif cmd == "/sell":        self._cmd_sell(chat_id, args)
            elif cmd == "/cancel":      self._cmd_cancel(chat_id)
            else:
                self._reply("Unknown command. Send /help for a full list of commands.")

        elif conv:
            self._handle_conversation(chat_id, text, conv)

    # ── Conversation router ───────────────────────────────────────────────────

    def _handle_conversation(self, chat_id: str, text: str, conv: dict) -> None:
        flow = conv["flow"]
        step = conv["step"]
        self._extend_conv(chat_id)

        if   flow == "buy"    and step == "qty":     self._buy_step_qty(chat_id, text, conv)
        elif flow == "buy"    and step == "sltp":    self._buy_step_sltp(chat_id, text, conv)
        elif flow == "buy"    and step == "confirm": self._buy_step_confirm(chat_id, text, conv)
        elif flow == "sell"   and step == "qty":     self._sell_step_qty(chat_id, text, conv)
        elif flow == "sell"   and step == "confirm": self._sell_step_confirm(chat_id, text, conv)
        elif flow == "cancel" and step == "select":  self._cancel_step_select(chat_id, text, conv)

    # ── /help ─────────────────────────────────────────────────────────────────

    def _cmd_help(self) -> None:
        self._reply(
            "📖 <b>Available Commands</b>\n"
            "\n"
            "ℹ️ <b>Info</b>\n"
            "/signals — latest bot signals for watchlist\n"
            "/news TICKER — recent news + sentiment\n"
            "/chart TICKER — technical indicators\n"
            "/summary — full account snapshot\n"
            "/history — last 10 closed trades\n"
            "/stats — performance stats (win rate, P&amp;L, Sharpe)\n"
            "/status — VPS service status\n"
            "/cash — available cash\n"
            "/positions — open positions with P&amp;L\n"
            "/orders — open orders\n"
            "/pnl — P&amp;L summary\n"
            "\n"
            "📋 <b>Watchlist</b>\n"
            "/watchlist — view active watchlist\n"
            "/add TICKER — add to watchlist\n"
            "/remove TICKER — remove from watchlist\n"
            "\n"
            "🌐 <b>Universe</b>\n"
            "/universe — view universe pool\n"
            "/universe add TICKER — add to universe\n"
            "/universe remove TICKER — remove from universe\n"
            "/universe refresh — rebuild from market data\n"
            "\n"
            "🔍 <b>Screening</b>\n"
            "/screen — run momentum screener now\n"
            "/screenstock TICKER — full 7-agent analysis\n"
            "\n"
            "💰 <b>Trading</b>\n"
            "/buy TICKER — place a buy order\n"
            "/sell TICKER — sell a position\n"
            "/cancel — cancel an open order\n"
            "\n"
            "⚙️ <b>Bot Control</b>\n"
            "/pause — pause trading (analysis continues)\n"
            "/resume — resume trading\n"
            "/killswitch on — halt all trading immediately\n"
            "/killswitch off — resume after kill switch"
        )

    # ── Info commands ─────────────────────────────────────────────────────────

    def _cmd_watchlist(self) -> None:
        tickers = self._read_file(self._watchlist_path)
        if not tickers:
            self._reply("📋 Watchlist is empty.\nUse /add TICKER to add tickers.")
            return
        lines = ["📋 <b>Active Watchlist:</b>"]
        for i, t in enumerate(tickers, 1):
            lines.append(f"{i}. {t}")
        lines.append("\nBot will trade these at next scheduled run.")
        self._reply("\n".join(lines))

    def _cmd_signals(self) -> None:
        tickers = self._read_file(self._watchlist_path)
        if not tickers:
            self._reply("📋 Watchlist is empty. Use /add TICKER to add tickers.")
            return

        lines     = ["📡 <b>Latest Signals:</b>"]
        last_time = None
        min_conf  = float(os.getenv("MIN_CONFIDENCE_SCORE", "7.0"))

        for ticker in tickers:
            files = sorted(
                _LOG_DIR.glob(f"{ticker}_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not files:
                lines.append(f"{ticker:<6}→ No analysis yet")
                continue
            try:
                data     = json.loads(files[0].read_text(encoding="utf-8"))
                decision = data.get("decision", {})
                action   = decision.get("action", "?")
                conf     = float(decision.get("confidence", 0.0))
                logged   = data.get("logged_at", "")

                if action in ("BUY", "SELL") and conf >= min_conf:
                    flag = "✅ (above threshold)"
                elif action == "HOLD":
                    flag = "⏸"
                else:
                    flag = ""

                lines.append(f"{ticker:<6}→ {action:<4} {conf:.1f}/10 {flag}")

                if logged and (last_time is None or logged > last_time):
                    last_time = logged
            except Exception:
                lines.append(f"{ticker:<6}→ Error reading log")

        if last_time:
            try:
                dt      = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                sgt_str = dt.astimezone(_SGT).strftime("%Y-%m-%d %H:%M SGT")
                lines.append(f"\nLast analysed: {sgt_str}")
            except Exception:
                pass

        self._reply("\n".join(lines))

    def _cmd_news(self, args: list) -> None:
        if not args:
            self._reply("Usage: /news TICKER (e.g. /news NVDA)")
            return
        ticker = args[0].upper()
        self._reply(f"📰 Fetching news for {ticker}…")

        try:
            from data.finnhub import FinnhubClient
            fh      = FinnhubClient()
            to_date = datetime.utcnow().strftime("%Y-%m-%d")
            from_dt = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
            news    = fh.get_company_news(ticker, from_dt, to_date)[:5]
            raw_sent = fh.get_news_sentiment(ticker)
        except Exception as exc:
            self._reply(f"❌ Finnhub unavailable: {exc}")
            return

        if not news:
            self._reply(f"📰 No recent news found for {ticker}.")
            return

        _POSITIVE = {"beat", "surge", "gain", "rise", "strong", "positive",
                     "upgrade", "buy", "bull", "record", "profit", "growth"}
        _NEGATIVE = {"miss", "fall", "drop", "decline", "weak", "negative",
                     "downgrade", "sell", "bear", "loss", "warning", "cut"}

        lines    = [f"📰 <b>{ticker} News:</b>"]
        positive = 0
        for i, article in enumerate(news, 1):
            headline = article.get("headline") or ""
            h_lower  = headline.lower()
            if any(w in h_lower for w in _POSITIVE):
                tone = "POSITIVE"
                positive += 1
            elif any(w in h_lower for w in _NEGATIVE):
                tone = "NEGATIVE"
            else:
                tone = "NEUTRAL"
            lines.append(f"{i}. [{tone}] {headline[:80]}")

        bull_pct = (raw_sent.get("sentiment") or {}).get("bullishPercent") or 0
        if bull_pct > 0.6:
            sent_label = f"🟢 Bullish ({bull_pct:.0%} positive)"
        elif bull_pct > 0.4:
            sent_label = f"🟡 Neutral ({bull_pct:.0%} positive)"
        else:
            sent_label = f"🔴 Bearish ({bull_pct:.0%} positive)"

        lines.append(f"Sentiment: {sent_label} ({positive}/{len(news)} positive)")
        self._reply("\n".join(lines))

    def _cmd_chart(self, args: list) -> None:
        if not args:
            self._reply("Usage: /chart TICKER (e.g. /chart NVDA)")
            return
        ticker = args[0].upper()
        self._reply(f"📈 Fetching chart data for {ticker}…")

        try:
            import pandas as pd
            import yfinance as yf

            hist = yf.Ticker(ticker).history(period="60d", interval="1d", auto_adjust=True)
            if hist.empty:
                self._reply(f"❌ No price data for {ticker}.")
                return

            close = hist["Close"]
            price = float(close.iloc[-1])
            vol   = float(hist["Volume"].iloc[-1])
            avg_vol  = float(hist["Volume"].rolling(20).mean().iloc[-1])
            vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0

            ema20 = float(close.ewm(span=20, min_periods=20).mean().iloc[-1])
            ema50_series = close.ewm(span=50, min_periods=50).mean()
            ema50_val = ema50_series.iloc[-1]
            ema50 = float(ema50_val) if pd.notna(ema50_val) else None

            # RSI(14) via Wilder smoothing
            delta    = close.diff()
            avg_gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
            avg_loss = (-delta).clip(lower=0).ewm(com=13, min_periods=14).mean()
            rs       = avg_gain / avg_loss.replace(0, float("nan"))
            rsi      = float((100 - 100 / (1 + rs)).iloc[-1])

            # MACD(12,26,9)
            ema12   = close.ewm(span=12, min_periods=12).mean()
            ema26   = close.ewm(span=26, min_periods=26).mean()
            macd    = ema12 - ema26
            sig_ln  = macd.ewm(span=9, min_periods=9).mean()
            macd_bullish = float(macd.iloc[-1]) > float(sig_ln.iloc[-1])

            support    = float(hist["Low"].rolling(20).min().iloc[-1])
            resistance = float(hist["High"].rolling(20).max().iloc[-1])

        except Exception as exc:
            self._reply(f"❌ Could not fetch chart data for {ticker}: {exc}")
            return

        if rsi > 70:
            rsi_label = "Overbought ⚠️"
        elif rsi < 30:
            rsi_label = "Oversold ⚠️"
        else:
            rsi_label = "Neutral"

        above20 = "✅" if price > ema20 else "❌"
        ema50_line = (
            f"EMA50:      ${ema50:,.2f} (price {'above' if price > ema50 else 'below'} "
            f"{'✅' if price > ema50 else '❌'})"
            if ema50 is not None else "EMA50:      n/a (< 50 bars)"
        )

        self._reply(
            f"📈 <b>{ticker} Technical Summary:</b>\n"
            f"Price:      ${price:,.2f}\n"
            f"RSI(14):    {rsi:.1f} — {rsi_label}\n"
            f"EMA20:      ${ema20:,.2f} (price {'above' if price > ema20 else 'below'} {above20})\n"
            f"{ema50_line}\n"
            f"MACD:       {'Bullish crossover ✅' if macd_bullish else 'Bearish crossover ❌'}\n"
            f"Support:    ${support:,.2f}\n"
            f"Resistance: ${resistance:,.2f}\n"
            f"Volume:     {vol_ratio:.1f}x average {'✅' if vol_ratio >= 1.5 else ''}"
        )

    def _cmd_summary(self) -> None:
        try:
            mm        = self._make_executor()
            positions = mm.get_positions()
            orders    = mm.get_open_orders()
            balance   = mm.get_account_balance()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        us        = balance.get("by_market", {}).get("US", {})
        cash      = us.get("cash", balance.get("cash", 0.0))
        equity    = us.get("total_assets", balance.get("portfolio_value", 0.0))
        today_pnl = sum(p.get("today_pnl", 0.0) for p in positions)

        if _KILL_SWITCH.exists():
            ks_status = "🔴 ON — trading halted"
        elif _PAUSE_LOCK.exists():
            ks_status = "⏸ PAUSED"
        else:
            ks_status = "✅ OFF"

        lines = [
            "📊 <b>Account Summary:</b>",
            f"💵 Cash:     ${cash:,.2f}",
            f"📈 Equity:   ${equity:,.2f}",
            f"📊 P&amp;L Today: ${_sign(today_pnl)}{today_pnl:,.2f}",
            "",
            f"📋 Positions: {len(positions)} open",
        ]
        for p in positions:
            ticker = p.get("display_ticker") or _display_ticker(p.get("ticker", ""))
            qty    = p.get("quantity", 0)
            upnl   = p.get("unrealised_pnl", 0.0)
            lines.append(
                f"  {ticker}: {qty} share{'s' if qty != 1 else ''}, "
                f"P&amp;L ${_sign(upnl)}{upnl:,.2f}"
            )

        lines.extend(["", f"📋 Orders: {len(orders)} pending"])
        for o in orders:
            ticker = o.get("ticker") or _display_ticker(o.get("code", ""))
            lines.append(
                f"  {o.get('action','')} {o.get('quantity',0)}x {ticker} "
                f"— {o.get('status','')}"
            )

        last_run = self._get_last_run_time()
        next_run = self._get_next_run_time()
        lines.extend([
            "",
            "🤖 <b>Bot Status:</b>",
            f"  Last run: {last_run}",
            f"  Next run: {next_run}",
            f"  Kill switch: {ks_status}",
        ])
        self._reply("\n".join(lines))

    def _cmd_history(self) -> None:
        if not _TRADES_CSV.exists():
            self._reply("📜 No trade history yet.")
            return

        rows: list = []
        try:
            with open(_TRADES_CSV, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rows.append(row)
        except Exception as exc:
            self._reply(f"❌ Could not read trade history: {exc}")
            return

        if not rows:
            self._reply("📜 No trade history yet.")
            return

        last10 = rows[-10:][::-1]
        lines  = ["📜 <b>Trade History (last 10):</b>"]
        for i, row in enumerate(last10, 1):
            ticker = row.get("ticker", "?")
            action = row.get("action", "?")
            qty    = row.get("quantity", "?")
            ts     = (row.get("timestamp") or "")[:10]
            try:
                price_str = f"${float(row.get('price', 0)):,.2f}"
            except (ValueError, TypeError):
                price_str = row.get("price", "?")
            lines.append(f"{i}. {ticker} {action} {qty}x @ {price_str} | {ts}")

        self._reply("\n".join(lines))

    def _cmd_stats(self) -> None:
        if not _TRADES_CSV.exists():
            self._reply("📊 No trade history yet.")
            return

        rows: list = []
        try:
            with open(_TRADES_CSV, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except Exception as exc:
            self._reply(f"❌ Could not read trade history: {exc}")
            return

        if not rows:
            self._reply("📊 No trade history yet.")
            return

        # ── Parse BUY/SELL entries, FIFO-match round trips ────────────
        total_placed = 0
        buy_queue: dict[str, list] = {}   # ticker → [(qty, price), ...]
        closed: list[tuple]        = []   # [(ticker, pnl), ...]

        for row in rows:
            action = (row.get("action") or "").upper().strip()
            ticker = (row.get("ticker") or "").strip()
            try:
                qty   = float(row.get("quantity") or 0)
                price = float(row.get("price")    or 0)
            except (ValueError, TypeError):
                continue

            if qty <= 0 or not ticker or price <= 0:
                continue

            if action == "BUY":
                total_placed += 1
                buy_queue.setdefault(ticker, []).append((qty, price))

            elif action == "SELL":
                total_placed += 1
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
                    closed.append((ticker, used * price - cost))

        # ── Build response ────────────────────────────────────────────
        lines = [
            "📊 <b>Bot Performance Stats:</b>",
            f"Total trades:     {total_placed}",
        ]

        if not closed:
            lines += [
                "",
                "No closed trades yet.",
                "(Win rate &amp; P&amp;L stats appear when positions are closed)",
            ]
            self._reply("\n".join(lines))
            return

        wins   = [(t, p) for t, p in closed if p > 0]
        losses = [(t, p) for t, p in closed if p <= 0]

        win_rate = len(wins) / len(closed) * 100
        avg_win  = sum(p for _, p in wins)   / len(wins)   if wins   else 0.0
        avg_loss = sum(p for _, p in losses) / len(losses) if losses else 0.0
        rr_ratio = abs(avg_win / avg_loss)                  if avg_loss else None

        best  = max(closed, key=lambda x: x[1])
        worst = min(closed, key=lambda x: x[1])

        # Streak — walk backward from the most recent closed trade
        streak_wins = streak_losses = 0
        if closed[-1][1] > 0:
            for _, p in reversed(closed):
                if p > 0:
                    streak_wins += 1
                else:
                    break
        else:
            for _, p in reversed(closed):
                if p <= 0:
                    streak_losses += 1
                else:
                    break
        streak_str = (
            f"{streak_wins} wins" if streak_wins > 0 else f"{streak_losses} losses"
        )

        rr_str = f"{rr_ratio:.1f}x" if rr_ratio is not None else "N/A (no losses yet)"

        lines += [
            f"Win rate:         {win_rate:.0f}% (target: &gt;50%)",
            f"Avg win:          $+{avg_win:,.2f}",
            f"Avg loss:         $-{abs(avg_loss):,.2f}",
            f"Risk/reward:      {rr_str}",
            f"Best trade:       $+{best[1]:,.2f} ({best[0]})",
            f"Worst trade:      ${_sign(worst[1])}{worst[1]:,.2f} ({worst[0]})",
            f"Current streak:   {streak_str}",
        ]

        # Sharpe ratio — meaningful only with >= 10 closed trades
        if len(closed) >= 10:
            import statistics
            pnl_vals = [p for _, p in closed]
            std = statistics.stdev(pnl_vals)
            if std > 0:
                sharpe = statistics.mean(pnl_vals) / std * (len(pnl_vals) ** 0.5)
                lines.append(f"Sharpe ratio:     {sharpe:.2f}")

        self._reply("\n".join(lines))

    def _cmd_status(self) -> None:
        services = {
            "Trading Bot":   "scheduler.py",
            "Price Monitor": "price_monitor.py",
            "FutuOpenD":     "FutuOpenD",
            "Dashboard":     "dashboard.py",
        }

        lines = ["🖥 <b>System Status:</b>"]
        for name, proc in services.items():
            try:
                result = subprocess.run(
                    ["pgrep", "-f", proc],
                    capture_output=True, timeout=5,
                )
                icon = "✅" if result.returncode == 0 else "❌"
                state = "running" if result.returncode == 0 else "not running"
                lines.append(f"{icon} {name:<16} {state}")
            except Exception:
                lines.append(f"❓ {name:<16} unknown")

        last_run = self._get_last_run_time()
        next_run = self._get_next_run_time()
        lines.extend([
            f"Last run:    {last_run}",
            f"Next run:    {next_run}",
        ])
        self._reply("\n".join(lines))

    def _cmd_orders(self) -> None:
        try:
            mm     = self._make_executor()
            orders = mm.get_open_orders()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        if not orders:
            self._reply("📋 No open orders.")
            return

        lines = ["📋 <b>Open Orders:</b>"]
        for i, o in enumerate(orders, 1):
            ticker = o.get("ticker") or _display_ticker(o.get("code", ""))
            action = o.get("action", "")
            qty    = o.get("quantity", 0)
            otype  = ("@ market" if "MARKET" in o.get("order_type", "").upper()
                      else f"@ ${o.get('price', 0):,.2f}")
            status = o.get("status", "")
            lines.append(
                f"{i}. {action} {qty:,}x {ticker} {otype} — {status}\n"
                f"   Order ID: <code>{o.get('order_id', '')}</code>"
            )
        self._reply("\n".join(lines))

    def _cmd_positions(self) -> None:
        try:
            mm        = self._make_executor()
            positions = mm.get_positions()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        if not positions:
            self._reply("📊 No open positions.")
            return

        lines      = ["📊 <b>Open Positions:</b>"]
        total_upnl = 0.0

        for i, p in enumerate(positions, 1):
            ticker  = p.get("display_ticker") or _display_ticker(p.get("ticker", ""))
            qty     = p.get("quantity", 0)
            entry   = p.get("entry_price", 0.0)
            tpnl    = p.get("today_pnl", 0.0)

            current = p.get("current_price") or 0.0
            price_label = ""
            if current <= 0:
                current, price_label = self._fetch_price_with_fallback(ticker, entry)

            # Always recalculate from first principles instead of trusting MooMoo's pl_val
            if entry > 0 and current > 0:
                upnl = (current - entry) * qty
            else:
                upnl = p.get("unrealised_pnl", 0.0)

            total_upnl += upnl
            price_str = f"${current:,.2f}" + (f" {price_label}" if price_label else "")

            lines.append(
                f"\n{i}. <b>{ticker}</b> — {qty:,} share{'s' if qty != 1 else ''}\n"
                f"   Avg Cost:    ${entry:,.2f}\n"
                f"   Current:     {price_str}\n"
                f"   Unreal P&amp;L: ${_sign(upnl)}{upnl:,.2f}\n"
                f"   Today P&amp;L:  ${_sign(tpnl)}{tpnl:,.2f}"
            )

        lines.append(
            f"\n<b>Total Unrealised P&amp;L: ${_sign(total_upnl)}{total_upnl:,.2f}</b>"
        )
        self._reply("\n".join(lines))

    def _cmd_pnl(self) -> None:
        try:
            mm        = self._make_executor()
            positions = mm.get_positions()
            balance   = mm.get_account_balance()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        today_total  = sum(p.get("today_pnl", 0.0)     for p in positions)
        unreal_total = sum(p.get("unrealised_pnl", 0.0) for p in positions)
        us           = balance.get("by_market", {}).get("US", {})
        equity       = us.get("total_assets", balance.get("portfolio_value", 0.0))

        lines = ["💰 <b>P&amp;L Summary</b>", "", "<b>Today:</b>"]
        for p in positions:
            ticker = p.get("display_ticker") or _display_ticker(p.get("ticker", ""))
            v      = p.get("today_pnl", 0.0)
            lines.append(f"  {ticker:<6}  ${_sign(v)}{v:,.2f}")
        lines.append(f"  Overall:  ${_sign(today_total)}{today_total:,.2f}")

        lines.extend(["", "<b>Unrealised:</b>"])
        for p in positions:
            ticker = p.get("display_ticker") or _display_ticker(p.get("ticker", ""))
            v      = p.get("unrealised_pnl", 0.0)
            lines.append(f"  {ticker:<6}  ${_sign(v)}{v:,.2f}")
        lines.append(f"  Total:    ${_sign(unreal_total)}{unreal_total:,.2f}")

        lines.extend(["", f"Account Equity: ${equity:,.2f}"])
        self._reply("\n".join(lines))

    def _cmd_cash(self) -> None:
        try:
            mm      = self._make_executor()
            balance = mm.get_account_balance()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        us   = balance.get("by_market", {}).get("US", {})
        cash = us.get("cash", balance.get("cash", 0.0))
        self._reply(f"💵 <b>Available Cash: ${cash:,.2f} USD</b>")

    # ── Watchlist / universe commands ─────────────────────────────────────────

    def _cmd_add(self, args: list) -> None:
        if not args:
            self._reply("Usage: /add TICKER1 TICKER2 …")
            return
        tickers = [t.upper() for t in args if t.replace(".", "").isalnum()]
        current = self._read_file(self._watchlist_path)
        added   = [t for t in tickers if t not in current]
        updated = current + added
        if added:
            self._write_file(self._watchlist_path, updated)
        self._reply(
            f"✅ <b>Watchlist updated</b>\n"
            f"Added: {', '.join(added) if added else '(already in watchlist)'}\n"
            f"Current watchlist: {', '.join(updated)}"
        )

    def _cmd_remove(self, args: list) -> None:
        if not args:
            self._reply("Usage: /remove TICKER1 TICKER2 …")
            return
        to_remove = {t.upper() for t in args}
        current   = self._read_file(self._watchlist_path)
        removed   = [t for t in current if t in to_remove]
        updated   = [t for t in current if t not in to_remove]
        if removed:
            self._write_file(self._watchlist_path, updated)
        self._reply(
            f"✅ <b>Watchlist updated</b>\n"
            f"Removed: {', '.join(removed) if removed else '(none found)'}\n"
            f"Current watchlist: {', '.join(updated) or '(empty)'}"
        )

    def _cmd_screen(self) -> None:
        self._reply("🔍 Running screener… results in ~30-60s")
        if self._screen_callback:
            threading.Thread(target=self._run_screen_safely,
                             name="screen-on-demand", daemon=True).start()
        else:
            self._reply("❌ Screener not available")

    def _cmd_screenstock(self, chat_id: str, args: list) -> None:
        if not args:
            self._reply("Usage: /screenstock TICKER (e.g. /screenstock NVDA)")
            return
        ticker = args[0].upper()
        self._reply(
            f"🔍 Running deep analysis on {ticker}…\n"
            "This takes 2-3 minutes ⏳"
        )
        threading.Thread(
            target=self._run_screenstock_safely,
            args=(ticker,),
            name=f"screenstock-{ticker}",
            daemon=True,
        ).start()

    def _cmd_universe(self, args: list) -> None:
        subcmd  = args[0].lower() if args else "list"
        subargs = args[1:]
        if   subcmd == "list":    self._universe_list()
        elif subcmd == "add":     self._universe_add(subargs)
        elif subcmd == "remove":  self._universe_remove(subargs)
        elif subcmd == "refresh": self._universe_refresh()
        else:
            self._reply(
                "Universe commands:\n"
                "/universe list\n"
                "/universe add TICKER …\n"
                "/universe remove TICKER …\n"
                "/universe refresh"
            )

    def _universe_list(self) -> None:
        current = self._read_file(self._universe_path)
        if current:
            self._reply(
                f"🌐 <b>Universe Pool ({len(current)} tickers):</b>\n"
                f"{', '.join(current)}"
            )
        else:
            self._reply("🌐 Universe is empty. Use /universe refresh to rebuild.")

    def _universe_add(self, args: list) -> None:
        if not args:
            self._reply("Usage: /universe add TICKER1 TICKER2 …")
            return
        tickers = [t.upper() for t in args if t.replace(".", "").isalnum()]
        current = self._read_file(self._universe_path)
        added   = [t for t in tickers if t not in current]
        updated = current + added
        if added:
            self._write_file(self._universe_path, updated)
        self._reply(
            f"✅ <b>Universe updated</b>\n"
            f"Added: {', '.join(added) if added else '(already in universe)'}\n"
            f"Universe size: {len(updated)} tickers"
        )

    def _universe_remove(self, args: list) -> None:
        if not args:
            self._reply("Usage: /universe remove TICKER1 TICKER2 …")
            return
        to_remove = {t.upper() for t in args}
        current   = self._read_file(self._universe_path)
        removed   = [t for t in current if t in to_remove]
        updated   = [t for t in current if t not in to_remove]
        if removed:
            self._write_file(self._universe_path, updated)
        self._reply(
            f"✅ <b>Universe updated</b>\n"
            f"Removed: {', '.join(removed) if removed else '(none found)'}\n"
            f"Universe size: {len(updated)} tickers"
        )

    def _universe_refresh(self) -> None:
        self._reply("🌐 Rebuilding universe from live market data… (~30s)")
        if self._universe_callback:
            threading.Thread(target=self._run_universe_safely,
                             name="universe-refresh", daemon=True).start()
        else:
            self._reply("❌ Universe refresh not configured")

    # ── Bot control commands ──────────────────────────────────────────────────

    def _cmd_pause(self) -> None:
        if _PAUSE_LOCK.exists():
            self._reply("⏸ Bot is already paused.\nSend /resume to re-enable trading.")
            return
        try:
            _PAUSE_LOCK.touch()
            self._reply(
                "⏸ <b>Bot paused</b> — analysis will continue but no new trades "
                "will be placed.\nSend /resume to re-enable trading."
            )
        except Exception as exc:
            self._reply(f"❌ Failed to pause: {exc}")

    def _cmd_resume(self) -> None:
        if not _PAUSE_LOCK.exists():
            self._reply("▶️ Bot is not paused. Trading is already active.")
            return
        try:
            _PAUSE_LOCK.unlink()
            self._reply("▶️ <b>Bot resumed</b> — trading re-enabled.")
        except Exception as exc:
            self._reply(f"❌ Failed to resume: {exc}")

    def _cmd_killswitch(self, args: list) -> None:
        subcmd = args[0].lower() if args else ""
        if subcmd == "on":
            if _KILL_SWITCH.exists():
                self._reply("🚨 Kill switch is already ACTIVE.")
                return
            try:
                _KILL_SWITCH.touch()
                self._reply(
                    "🚨 <b>Kill switch ACTIVATED</b>\n"
                    "All trading halted immediately.\n"
                    "Send /killswitch off to resume."
                )
            except Exception as exc:
                self._reply(f"❌ Failed: {exc}")
        elif subcmd == "off":
            if not _KILL_SWITCH.exists():
                self._reply("✅ Kill switch is already OFF.")
                return
            try:
                _KILL_SWITCH.unlink()
                self._reply("✅ <b>Kill switch DEACTIVATED</b>\nTrading resumed.")
            except Exception as exc:
                self._reply(f"❌ Failed: {exc}")
        else:
            self._reply(
                "Usage:\n"
                "/killswitch on  — halt all trading\n"
                "/killswitch off — resume trading"
            )

    # ── /buy flow ─────────────────────────────────────────────────────────────

    def _cmd_buy(self, chat_id: str, args: list) -> None:
        if not args:
            self._reply("Usage: /buy TICKER (e.g. /buy NVDA)")
            return
        ticker = args[0].upper()

        if self._trading_halted():
            self._reply(self._trading_halted_msg())
            return

        self._reply(f"⏳ Fetching price for {ticker}…")
        try:
            price, atr = self._get_price_and_atr(ticker)
        except Exception as exc:
            self._reply(f"❌ Could not fetch price for {ticker}: {exc}")
            return

        if price <= 0:
            self._reply(f"❌ No price data for {ticker}. Check the ticker symbol.")
            return

        self._start_conv(chat_id, "buy", "qty",
                         ticker=ticker, price=price, atr=atr)

        self._reply(
            f"🟢 <b>BUY {ticker}</b>\n"
            f"Current Price: ${price:,.2f}\n\n"
            f"How many shares do you want to buy?"
        )

    def _buy_step_qty(self, chat_id: str, text: str, conv: dict) -> None:
        try:
            qty = int(text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._reply("❌ Please enter a whole number greater than 0.")
            return

        ticker = conv["ticker"]
        price  = conv["price"]
        atr    = conv["atr"]

        if atr > 0:
            sl_price = price - atr * _ATR_SL_MULT
            tp_price = price + atr * _ATR_TP_MULT
            sl_pct   = (price - sl_price) / price * 100
            tp_pct   = (tp_price - price) / price * 100
            sltp_line = (
                f"Suggested (ATR-based):\n"
                f"  SL: ${sl_price:,.2f} (-{sl_pct:.1f}%)\n"
                f"  TP: ${tp_price:,.2f} (+{tp_pct:.1f}%)"
            )
        else:
            sl_price = price * 0.97
            tp_price = price * 1.06
            sl_pct, tp_pct = 3.0, 6.0
            sltp_line = (
                f"Suggested (fixed 3%/6%):\n"
                f"  SL: ${sl_price:,.2f} (-{sl_pct:.1f}%)\n"
                f"  TP: ${tp_price:,.2f} (+{tp_pct:.1f}%)"
            )

        total = price * qty
        conv.update(quantity=qty, sl_price=sl_price, tp_price=tp_price)
        conv["step"] = "sltp"

        self._reply(
            f"🟢 <b>BUY {qty:,}x {ticker} @ ~${price:,.2f}</b>\n"
            f"Total value: ~${total:,.2f}\n\n"
            f"Stop Loss / Take Profit:\n{sltp_line}\n\n"
            f"Reply:\n"
            f"  <b>standard</b> — use suggested SL/TP above\n"
            f"  <b>XX YY</b> — enter your own SL% and TP%\n"
            f"              e.g. '2 5' = 2% SL, 5% TP"
        )

    def _buy_step_sltp(self, chat_id: str, text: str, conv: dict) -> None:
        ticker = conv["ticker"]
        price  = conv["price"]
        qty    = conv["quantity"]

        raw = text.strip().lower()
        if raw == "standard":
            sl_price = conv["sl_price"]
            tp_price = conv["tp_price"]
        else:
            parts = raw.split()
            if len(parts) != 2:
                self._reply(
                    "❌ Enter 'standard' or two numbers like '2 5' (SL% TP%)."
                )
                return
            try:
                sl_pct = float(parts[0])
                tp_pct = float(parts[1])
                if sl_pct <= 0 or tp_pct <= 0:
                    raise ValueError
            except ValueError:
                self._reply("❌ Both values must be positive numbers, e.g. '2 5'.")
                return
            sl_price = price * (1 - sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100)

        conv.update(sl_price=sl_price, tp_price=tp_price)
        conv["step"] = "confirm"

        sl_pct_disp = (price - sl_price) / price * 100
        tp_pct_disp = (tp_price - price) / price * 100

        self._reply(
            f"🟢 <b>Confirm Order:</b>\n"
            f"  BUY {qty:,}x {ticker} @ market\n"
            f"  Stop Loss:   ${sl_price:,.2f} (-{sl_pct_disp:.1f}%)\n"
            f"  Take Profit: ${tp_price:,.2f} (+{tp_pct_disp:.1f}%)\n\n"
            f"Reply <b>confirm</b> to place or <b>cancel</b> to abort."
        )

    def _buy_step_confirm(self, chat_id: str, text: str, conv: dict) -> None:
        raw = text.strip().lower()
        if raw == "cancel":
            self._end_conv(chat_id)
            self._reply("❌ BUY order cancelled.")
            return
        if raw != "confirm":
            self._reply("Reply <b>confirm</b> to place or <b>cancel</b> to abort.")
            return

        if self._trading_halted():
            self._end_conv(chat_id)
            self._reply(self._trading_halted_msg())
            return

        ticker   = conv["ticker"]
        qty      = conv["quantity"]
        price    = conv["price"]
        sl_price = conv["sl_price"]
        tp_price = conv["tp_price"]
        self._end_conv(chat_id)

        try:
            mm    = self._make_executor()
            order = mm.place_order(ticker=ticker, action="BUY", quantity=qty)
            if order is None:
                self._reply(f"❌ Order submission failed for {ticker}.")
                mm.close()
                return

            oid = order.get("order_id") or order.get("id", "—")

            sl_oid = tp_oid = ""
            try:
                sl_ord = mm.place_order(ticker=ticker, action="SELL",
                                        quantity=qty, order_type="limit", price=sl_price)
                if sl_ord:
                    sl_oid = sl_ord.get("order_id", "")
            except Exception as exc:
                logger.warning("[TelegramListener] SL bracket failed: %s", exc)
            try:
                tp_ord = mm.place_order(ticker=ticker, action="SELL",
                                        quantity=qty, order_type="limit", price=tp_price)
                if tp_ord:
                    tp_oid = tp_ord.get("order_id", "")
            except Exception as exc:
                logger.warning("[TelegramListener] TP bracket failed: %s", exc)

            mm.close()

            self._reply(
                f"✅ <b>Order Placed: BUY {qty:,}x {ticker}</b>\n"
                f"Order ID: <code>{oid}</code>\n"
                f"SL: ${sl_price:,.2f}  TP: ${tp_price:,.2f}"
                + (f"\nSL order: <code>{sl_oid}</code>" if sl_oid else "")
                + (f"\nTP order: <code>{tp_oid}</code>" if tp_oid else "")
            )

        except Exception as exc:
            logger.error("[TelegramListener] /buy execute error: %s", exc, exc_info=True)
            self._reply(f"❌ Order error: {exc}")

    # ── /sell flow ────────────────────────────────────────────────────────────

    def _cmd_sell(self, chat_id: str, args: list) -> None:
        if not args:
            self._reply("Usage: /sell TICKER (e.g. /sell NVDA)")
            return
        ticker = args[0].upper()

        if self._trading_halted():
            self._reply(self._trading_halted_msg())
            return

        try:
            mm        = self._make_executor()
            positions = mm.get_positions()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        pos = next(
            (p for p in positions
             if (p.get("display_ticker") or _display_ticker(p.get("ticker", "")))
             == ticker),
            None,
        )

        if pos is None:
            self._reply(
                f"❌ You don't hold any {ticker} shares.\n"
                "Cannot sell what you don't own (no short selling)."
            )
            return

        held    = pos.get("quantity", 0)
        entry   = pos.get("entry_price", 0.0)
        current = pos.get("current_price", 0.0)
        upnl    = pos.get("unrealised_pnl", 0.0)

        self._start_conv(chat_id, "sell", "qty",
                         ticker=ticker, price=current, held=held,
                         entry_price=entry, unrealised_pnl=upnl)

        self._reply(
            f"🔴 <b>SELL {ticker}</b>\n"
            f"Current Price: ${current:,.2f}\n"
            f"You hold: {held:,} share{'s' if held != 1 else ''} "
            f"(avg cost ${entry:,.2f})\n"
            f"Unrealised P&amp;L: ${_sign(upnl)}{upnl:,.2f}\n\n"
            f"How many shares to sell? (max {held:,})"
        )

    def _sell_step_qty(self, chat_id: str, text: str, conv: dict) -> None:
        ticker = conv["ticker"]
        held   = conv["held"]
        price  = conv["price"]

        try:
            qty = int(text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self._reply("❌ Please enter a whole number greater than 0.")
            return

        if qty > held:
            self._reply(
                f"❌ You only hold {held:,} share{'s' if held != 1 else ''} of {ticker}.\n"
                f"Please enter {held:,} or less."
            )
            return

        conv["quantity"] = qty
        conv["step"]     = "confirm"
        est_value        = price * qty

        self._reply(
            f"🔴 <b>Confirm Order:</b>\n"
            f"  SELL {qty:,}x {ticker} @ market\n"
            f"  Current Price: ~${price:,.2f}\n"
            f"  Est. Value: ~${est_value:,.2f}\n\n"
            f"Reply <b>confirm</b> to place or <b>cancel</b> to abort."
        )

    def _sell_step_confirm(self, chat_id: str, text: str, conv: dict) -> None:
        raw = text.strip().lower()
        if raw == "cancel":
            self._end_conv(chat_id)
            self._reply("❌ SELL order cancelled.")
            return
        if raw != "confirm":
            self._reply("Reply <b>confirm</b> to place or <b>cancel</b> to abort.")
            return

        if self._trading_halted():
            self._end_conv(chat_id)
            self._reply(self._trading_halted_msg())
            return

        ticker = conv["ticker"]
        qty    = conv["quantity"]
        self._end_conv(chat_id)

        try:
            mm    = self._make_executor()
            order = mm.place_order(ticker=ticker, action="SELL", quantity=qty)
            mm.close()

            if order is None:
                self._reply(f"❌ Order submission failed for {ticker}.")
                return

            oid = order.get("order_id") or order.get("id", "—")
            self._reply(
                f"✅ <b>Order Placed: SELL {qty:,}x {ticker}</b>\n"
                f"Order ID: <code>{oid}</code>"
            )
        except Exception as exc:
            logger.error("[TelegramListener] /sell execute error: %s", exc, exc_info=True)
            self._reply(f"❌ Order error: {exc}")

    # ── /cancel flow ──────────────────────────────────────────────────────────

    def _cmd_cancel(self, chat_id: str) -> None:
        try:
            mm     = self._make_executor()
            orders = mm.get_open_orders()
            mm.close()
        except Exception as exc:
            self._reply(f"❌ MooMoo unavailable: {exc}")
            return

        if not orders:
            self._reply("📋 No open orders to cancel.")
            return

        lines = ["Which order to cancel?"]
        for i, o in enumerate(orders, 1):
            ticker = o.get("ticker") or _display_ticker(o.get("code", ""))
            action = o.get("action", "")
            qty    = o.get("quantity", 0)
            status = o.get("status", "")
            lines.append(f"{i}. {action} {qty:,}x {ticker} — {status}")
        lines.append("\nReply with number (e.g. '1')")

        self._start_conv(chat_id, "cancel", "select", orders=orders)
        self._reply("\n".join(lines))

    def _cancel_step_select(self, chat_id: str, text: str, conv: dict) -> None:
        orders = conv.get("orders", [])
        try:
            idx = int(text.strip()) - 1
            if not (0 <= idx < len(orders)):
                raise ValueError
        except ValueError:
            self._reply(f"❌ Enter a number between 1 and {len(orders)}.")
            return

        o      = orders[idx]
        oid    = o.get("order_id", "")
        ticker = o.get("ticker") or _display_ticker(o.get("code", ""))
        action = o.get("action", "")
        qty    = o.get("quantity", 0)
        self._end_conv(chat_id)

        try:
            mm      = self._make_executor()
            success = mm.cancel_order(oid)
            mm.close()
        except Exception as exc:
            self._reply(f"❌ Cancel failed: {exc}")
            return

        if success:
            self._reply(
                f"✅ <b>Cancelled: {action} {qty:,}x {ticker}</b>\n"
                f"Order ID: <code>{oid}</code>"
            )
        else:
            self._reply(
                f"❌ Cancel failed for order <code>{oid}</code>. "
                "It may have already filled or expired."
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_price_with_fallback(
        self, ticker: str, last_known: float
    ) -> tuple[float, str]:
        """
        Fetch a live price from Finnhub when MooMoo returns $0.00.

        Returns (price, label) where label is:
          ""             — fresh Finnhub price
          "(last known)" — Finnhub unavailable; using last_known (entry price)
        """
        try:
            from data.finnhub import FinnhubClient
            quote = FinnhubClient().get_quote(ticker)
            price = float(quote.get("c") or 0)
            if price > 0:
                logger.debug(
                    "[TelegramListener] Finnhub price fallback: %s → $%.2f", ticker, price
                )
                return price, ""
        except Exception as exc:
            logger.warning(
                "[TelegramListener] Finnhub price fetch failed for %s: %s", ticker, exc
            )

        if last_known > 0:
            logger.warning(
                "[TelegramListener] Using entry price as last-known for %s: $%.2f",
                ticker, last_known,
            )
            return last_known, "(last known)"
        return 0.0, ""

    def _make_executor(self):
        from execution.moomoo import MooMooConnector
        return MooMooConnector()

    def _get_price_and_atr(self, ticker: str) -> tuple[float, float]:
        """Fetch current price and ATR(14) via yfinance."""
        import pandas as pd
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="30d", interval="1d", auto_adjust=True)
        if hist.empty:
            raise ValueError(f"No price data returned for {ticker}")

        price = float(hist["Close"].iloc[-1])
        if len(hist) < 15:
            return price, 0.0

        close_prev = hist["Close"].shift(1)
        tr = pd.concat([
            hist["High"] - hist["Low"],
            (hist["High"] - close_prev).abs(),
            (hist["Low"]  - close_prev).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.ewm(com=13, min_periods=14).mean().iloc[-1])
        return price, atr

    def _kill_switch_active(self) -> bool:
        return _KILL_SWITCH.exists()

    def _pause_active(self) -> bool:
        return _PAUSE_LOCK.exists()

    def _trading_halted(self) -> bool:
        return _KILL_SWITCH.exists() or _PAUSE_LOCK.exists()

    def _trading_halted_msg(self) -> str:
        if _KILL_SWITCH.exists():
            return (
                "🚫 Trading is halted (kill switch active).\n"
                "Send /killswitch off to re-enable."
            )
        if _PAUSE_LOCK.exists():
            return (
                "🚫 Trading is paused.\n"
                "Send /resume to re-enable."
            )
        return ""

    def _get_last_run_time(self) -> str:
        """Return the most recent log file's timestamp as SGT string."""
        try:
            files = list(_LOG_DIR.glob("*.json"))
            if not files:
                return "No runs yet"
            latest = max(files, key=lambda p: p.stat().st_mtime)
            mtime  = datetime.fromtimestamp(latest.stat().st_mtime, tz=_SGT)
            return mtime.strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            return "Unknown"

    def _get_next_run_time(self) -> str:
        """Return the next scheduled run time (SGT) based on hardcoded schedule."""
        try:
            now = datetime.now(_SGT)
            # Build candidate datetimes for today and tomorrow
            candidates: list[datetime] = []
            for day_offset in range(7):
                candidate_date = now.date() + timedelta(days=day_offset)
                weekday = candidate_date.weekday()  # 0=Mon, 6=Sun
                if weekday >= 5:
                    continue
                for hour, minute in _SCHEDULE_TIMES:
                    dt = datetime(
                        candidate_date.year, candidate_date.month, candidate_date.day,
                        hour, minute, tzinfo=_SGT,
                    )
                    if dt > now:
                        candidates.append(dt)

            if not candidates:
                return "Unknown"
            next_dt = min(candidates)
            return next_dt.strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            return "Unknown"

    def _start_conv(self, chat_id: str, flow: str, step: str, **data) -> None:
        self._conversations[chat_id] = {
            "flow":       flow,
            "step":       step,
            "expires_at": time.time() + _CONV_TIMEOUT,
            **data,
        }

    def _end_conv(self, chat_id: str) -> None:
        self._conversations.pop(chat_id, None)

    def _extend_conv(self, chat_id: str) -> None:
        if chat_id in self._conversations:
            self._conversations[chat_id]["expires_at"] = time.time() + _CONV_TIMEOUT

    def _cleanup_expired(self) -> None:
        now     = time.time()
        expired = [cid for cid, c in self._conversations.items()
                   if c["expires_at"] < now]
        for cid in expired:
            del self._conversations[cid]

    def _run_screen_safely(self) -> None:
        try:
            self._screen_callback()
        except Exception as exc:
            logger.error("[TelegramListener] /screen error: %s", exc, exc_info=True)
            self._reply(f"❌ Screener error: {exc}")

    def _run_universe_safely(self) -> None:
        try:
            self._universe_callback()
        except Exception as exc:
            logger.error("[TelegramListener] /universe refresh error: %s", exc, exc_info=True)
            self._reply(f"❌ Universe refresh error: {exc}")

    def _run_screenstock_safely(self, ticker: str) -> None:
        try:
            from data.fetcher import DataFetcher
            from agents.trading_agents import TradingAgentsWrapper

            data     = DataFetcher().fetch(ticker)
            decision = TradingAgentsWrapper().analyse(data)

            action = decision.get("action", "?")
            conf   = float(decision.get("confidence", 0.0))
            flags  = decision.get("risk_flags", [])

            # Risk level
            _CRITICAL = {"halt", "delist", "fraud", "bankruptcy"}
            if any(any(c in f.lower() for c in _CRITICAL) for f in flags):
                risk = "HIGH ⚠️"
            elif conf >= 8.0:
                risk = "LOW"
            else:
                risk = "MEDIUM"

            # Technical summary from raw data
            tech      = data.get("technicals", {}) or {}
            rsi       = tech.get("rsi_14")
            macd_val  = tech.get("macd")
            macd_sig  = tech.get("macd_signal")
            tech_parts: list[str] = []
            if rsi is not None:
                if rsi > 70:
                    tech_parts.append(f"RSI {rsi:.0f} overbought")
                elif rsi < 30:
                    tech_parts.append(f"RSI {rsi:.0f} oversold")
                else:
                    tech_parts.append(f"RSI {rsi:.0f}")
            if macd_val is not None and macd_sig is not None:
                tech_parts.append(
                    "MACD bullish" if macd_val > macd_sig else "MACD bearish"
                )
            tech_str = ", ".join(tech_parts) if tech_parts else "data unavailable"

            # Sentiment summary
            sent     = data.get("sentiment", {}) or {}
            bull_pct = sent.get("score") or 0
            if bull_pct > 0.6:
                sent_str = f"Bullish ({bull_pct:.0%})"
            elif bull_pct > 0.4:
                sent_str = "Neutral"
            else:
                sent_str = f"Bearish ({bull_pct:.0%})"

            icon = "🟢" if action == "BUY" else ("🔴" if action == "SELL" else "⏸")

            lines = [
                f"🔍 <b>Deep Analysis: {ticker}</b>",
                "",
                f"Confidence:  {conf:.1f}/10",
                f"Signal:      {icon} {action}",
                f"Risk:        {risk}",
                "",
                "Agent Summary:",
                f"📈 Technical:    {tech_str}",
                f"📰 Sentiment:    {sent_str}",
                f"🐂 Bull case:    {(decision.get('bull_case') or 'N/A')[:100]}",
                f"🐻 Bear case:    {(decision.get('bear_case') or 'N/A')[:100]}",
                f"✅ Fund Manager: {action} with {conf:.1f} confidence",
            ]

            reasoning = (decision.get("reasoning") or "").strip()
            if reasoning:
                lines.append(f"\n{reasoning[:200]}")

            if flags:
                lines.append(f"\n⚠️ Risk flags: {', '.join(flags[:3])}")

            if action in ("BUY", "SELL"):
                lines.extend(["", "Would you like to:"])
                if action == "BUY":
                    lines.append(f"/buy {ticker} — place a trade now")
                    lines.append(f"/add {ticker} — add to active watchlist")
                else:
                    lines.append(f"/sell {ticker} — place a sell order")

            self._reply("\n".join(lines))

        except Exception as exc:
            logger.error("[TelegramListener] /screenstock error: %s", exc, exc_info=True)
            self._reply(f"❌ Analysis failed for {ticker}: {exc}")

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _read_file(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [
            ln.split("#")[0].strip().upper()
            for ln in lines
            if ln.split("#")[0].strip()
        ]

    def _write_file(self, path: Path, tickers: list[str]) -> None:
        path.write_text("\n".join(tickers) + "\n", encoding="utf-8")
        logger.info("[TelegramListener] %s updated (%d tickers)", path.name, len(tickers))

    def _read_watchlist(self) -> list[str]:
        return self._read_file(self._watchlist_path)

    def _write_watchlist(self, tickers: list[str]) -> None:
        self._write_file(self._watchlist_path, tickers)

    # ── Reply ─────────────────────────────────────────────────────────────────

    def _reply(self, text: str) -> None:
        if not self._token or not self._chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("[TelegramListener] Reply failed: %s", exc)
