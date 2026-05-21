"""
TelegramCommandListener — long-polls Telegram getUpdates and handles
watchlist management commands sent from your configured chat.

Active watchlist commands (watchlist.txt — tickers the bot trades):
    /add TICKER1 TICKER2 …   — add tickers to watchlist.txt
    /remove TICKER1 …        — remove tickers from watchlist.txt
    /watchlist               — show current watchlist.txt

Screening universe commands (watchlist_universe.txt — 50-ticker pool):
    /universe list           — show current universe
    /universe add TICKER …   — manually add tickers to universe
    /universe remove TICKER  — manually remove tickers from universe

On-demand triggers:
    /screen                  — run the momentum screener right now
    /universe refresh        — rebuild universe from live market data

Security: only messages from TELEGRAM_CHAT_ID are processed.

Usage:
    from monitoring.telegram_listener import TelegramCommandListener
    listener = TelegramCommandListener(
        screen_callback=scheduler.run_daily_screen,
        universe_callback=scheduler.universe_update_cycle,
    )
    listener.listen_in_background()
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_WATCHLIST_PATH  = Path(__file__).resolve().parent.parent / "watchlist.txt"
_UNIVERSE_PATH   = Path(__file__).resolve().parent.parent / "watchlist_universe.txt"
_POLL_TIMEOUT    = 30   # long-poll window (seconds)
_REQUEST_TIMEOUT = 35   # requests.get timeout — must exceed _POLL_TIMEOUT
_RETRY_WAIT      = 10   # back-off after a poll error


class TelegramCommandListener:
    """
    Long-polls Telegram and dispatches /commands to handler methods.

    Instantiate once and call listen_in_background() to start the daemon thread.
    The thread runs until the process exits.
    """

    def __init__(
        self,
        screen_callback: Optional[Callable] = None,
        universe_callback: Optional[Callable] = None,
        watchlist_path: Path = _WATCHLIST_PATH,
        universe_path: Path = _UNIVERSE_PATH,
    ):
        self._token            = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id          = os.getenv("TELEGRAM_CHAT_ID", "")
        self._offset           = 0
        self._screen_callback  = screen_callback
        self._universe_callback = universe_callback
        self._watchlist_path   = Path(watchlist_path)
        self._universe_path    = Path(universe_path)

        if self._token and self._chat_id:
            logger.info("[TelegramListener] Configured — chat_id=%s", self._chat_id)
        else:
            logger.warning(
                "[TelegramListener] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set "
                "— command listener disabled"
            )

    # ── Public ────────────────────────────────────────────────────────────────

    def listen_in_background(self) -> threading.Thread:
        """Spawn a daemon thread and return it."""
        t = threading.Thread(target=self.start, name="telegram-listener", daemon=True)
        t.start()
        logger.info("[TelegramListener] Background thread started")
        return t

    def start(self) -> None:
        """
        Blocking long-poll loop.
        Call from a dedicated thread (or use listen_in_background()).
        """
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
        data    = resp.json()
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, update: dict) -> None:
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text    = (message.get("text") or "").strip()

        # Only process messages from our authorised chat
        if chat_id != self._chat_id:
            return
        if not text.startswith("/"):
            return

        parts = text.split()
        cmd   = parts[0].lower().split("@")[0]   # strip @botname suffix
        args  = parts[1:]

        logger.info("[TelegramListener] cmd=%s  args=%s", cmd, args)

        if cmd == "/add":
            self._cmd_add(args)
        elif cmd == "/remove":
            self._cmd_remove(args)
        elif cmd == "/watchlist":
            self._cmd_watchlist()
        elif cmd == "/screen":
            self._cmd_screen()
        elif cmd == "/universe":
            self._cmd_universe(args)

    # ── Command handlers ──────────────────────────────────────────────────────

    def _cmd_add(self, args: list) -> None:
        if not args:
            self._reply("Usage: /add TICKER1 TICKER2 …")
            return
        tickers = [t.upper() for t in args if t.isalpha() or "." in t]
        current = self._read_watchlist()
        added   = [t for t in tickers if t not in current]
        updated = current + added
        if added:
            self._write_watchlist(updated)
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
        current   = self._read_watchlist()
        removed   = [t for t in current if t in to_remove]
        updated   = [t for t in current if t not in to_remove]
        if removed:
            self._write_watchlist(updated)
        self._reply(
            f"✅ <b>Watchlist updated</b>\n"
            f"Removed: {', '.join(removed) if removed else '(none found)'}\n"
            f"Current watchlist: {', '.join(updated) or '(empty)'}"
        )

    def _cmd_watchlist(self) -> None:
        current = self._read_watchlist()
        if current:
            self._reply(
                f"📋 <b>Current watchlist</b> ({len(current)} tickers)\n"
                f"{', '.join(current)}"
            )
        else:
            self._reply("📋 Watchlist is empty.\nUse /add TICKER to add tickers.")

    def _cmd_screen(self) -> None:
        self._reply("🔍 Running screener… results in ~30-60s")
        if self._screen_callback:
            threading.Thread(
                target=self._run_screen_safely,
                name="screen-on-demand",
                daemon=True,
            ).start()
        else:
            self._reply("❌ Screener not available")

    def _cmd_universe(self, args: list) -> None:
        """
        /universe list               — show watchlist_universe.txt
        /universe add TICKER …      — add to watchlist_universe.txt
        /universe remove TICKER …   — remove from watchlist_universe.txt
        /universe refresh            — rebuild universe from live market data
        """
        subcmd = args[0].lower() if args else "list"
        subargs = args[1:]

        if subcmd == "list":
            self._cmd_universe_list()
        elif subcmd == "add":
            self._cmd_universe_add(subargs)
        elif subcmd == "remove":
            self._cmd_universe_remove(subargs)
        elif subcmd == "refresh":
            self._cmd_universe_refresh()
        else:
            self._reply(
                "Universe commands:\n"
                "/universe list — show universe\n"
                "/universe add TICKER … — add tickers\n"
                "/universe remove TICKER … — remove tickers\n"
                "/universe refresh — rebuild from live data"
            )

    def _cmd_universe_list(self) -> None:
        current = self._read_file(self._universe_path)
        if current:
            self._reply(
                f"🌐 <b>Screening universe</b> ({len(current)} tickers)\n"
                f"{', '.join(current)}"
            )
        else:
            self._reply(
                "🌐 Universe is empty.\n"
                "Use /universe add TICKER or /universe refresh to rebuild."
            )

    def _cmd_universe_add(self, args: list) -> None:
        if not args:
            self._reply("Usage: /universe add TICKER1 TICKER2 …")
            return
        tickers = [t.upper() for t in args if t.isalpha() or "." in t]
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

    def _cmd_universe_remove(self, args: list) -> None:
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

    def _cmd_universe_refresh(self) -> None:
        self._reply("🌐 Rebuilding universe from live market data… (~30s)")
        if self._universe_callback:
            threading.Thread(
                target=self._run_universe_safely,
                name="universe-refresh",
                daemon=True,
            ).start()
        else:
            self._reply("❌ Universe refresh not configured")

    def _run_universe_safely(self) -> None:
        try:
            self._universe_callback()
        except Exception as exc:
            logger.error("[TelegramListener] /universe refresh error: %s", exc, exc_info=True)
            self._reply(f"❌ Universe refresh error: {exc}")

    def _run_screen_safely(self) -> None:
        try:
            self._screen_callback()
        except Exception as exc:
            logger.error("[TelegramListener] /screen error: %s", exc, exc_info=True)
            self._reply(f"❌ Screener error: {exc}")

    # ── File I/O helpers ──────────────────────────────────────────────────────

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

    # Convenience aliases used by the /add /remove /watchlist handlers
    def _read_watchlist(self) -> list[str]:
        return self._read_file(self._watchlist_path)

    def _write_watchlist(self, tickers: list[str]) -> None:
        self._write_file(self._watchlist_path, tickers)

    # ── Reply helper ──────────────────────────────────────────────────────────

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
