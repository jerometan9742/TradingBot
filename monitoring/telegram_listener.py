"""
TelegramCommandListener — long-polls Telegram getUpdates and handles
watchlist management commands sent from your configured chat.

Commands:
    /add TICKER1 TICKER2 …   — add tickers to watchlist.txt
    /remove TICKER1 …        — remove tickers from watchlist.txt
    /watchlist               — show current watchlist.txt
    /screen                  — run the momentum screener immediately

Security: only messages from TELEGRAM_CHAT_ID are processed.

Usage:
    from monitoring.telegram_listener import TelegramCommandListener
    listener = TelegramCommandListener(screen_callback=scheduler.run_daily_screen)
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
        watchlist_path: Path = _WATCHLIST_PATH,
    ):
        self._token          = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id        = os.getenv("TELEGRAM_CHAT_ID", "")
        self._offset         = 0
        self._screen_callback = screen_callback
        self._watchlist_path  = Path(watchlist_path)

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

    def _run_screen_safely(self) -> None:
        try:
            self._screen_callback()
        except Exception as exc:
            logger.error("[TelegramListener] /screen error: %s", exc, exc_info=True)
            self._reply(f"❌ Screener error: {exc}")

    # ── Watchlist file I/O ────────────────────────────────────────────────────

    def _read_watchlist(self) -> list[str]:
        if not self._watchlist_path.exists():
            return []
        lines = self._watchlist_path.read_text(encoding="utf-8").splitlines()
        return [
            ln.split("#")[0].strip().upper()
            for ln in lines
            if ln.split("#")[0].strip()
        ]

    def _write_watchlist(self, tickers: list[str]) -> None:
        self._watchlist_path.write_text("\n".join(tickers) + "\n", encoding="utf-8")
        logger.info("[TelegramListener] watchlist.txt updated (%d tickers)", len(tickers))

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
