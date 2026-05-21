"""
WatchlistManager — reads tickers from watchlist.txt with live-reload support.

File format:
    # Lines starting with # are comments
    AAPL
    MSFT
    D05.SI   # inline comments also stripped

Falls back to the WATCHLIST env variable if watchlist.txt is not found.
Hot-reloads automatically: get_tickers() re-reads the file whenever its
mtime changes, so edits take effect on the next analysis cycle without
restarting the scheduler.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "watchlist.txt"


class WatchlistManager:
    """
    Manages the trading watchlist from a text file with hot-reload.

    Tracks the file's mtime; calling get_tickers() costs one stat() call
    when the file is unchanged — zero re-parsing overhead per cycle.
    """

    def __init__(self, path: Path = _DEFAULT_PATH):
        self._path = Path(path)
        self._tickers: list[str] = []
        self._mtime: float = 0.0
        self._load()

    def get_tickers(self) -> list[str]:
        """
        Return the current watchlist.

        Auto-reloads if watchlist.txt has been modified since last read.
        """
        self._reload_if_changed()
        return list(self._tickers)

    def __repr__(self) -> str:
        return f"WatchlistManager(path={self._path}, tickers={self._tickers})"

    # ── Private ───────────────────────────────────────────────────────────────

    def _reload_if_changed(self) -> None:
        if not self._path.exists():
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            logger.info("[WatchlistManager] File changed — reloading %s", self._path.name)
            self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._tickers = self._load_from_env()
            return

        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            tickers = []
            for raw in lines:
                # Strip inline comments then whitespace
                line = raw.split("#")[0].strip().upper()
                if line:
                    tickers.append(line)

            if tickers:
                self._tickers = tickers
                self._mtime = self._path.stat().st_mtime
                logger.info(
                    "[WatchlistManager] Loaded %d ticker(s) from %s: %s",
                    len(tickers), self._path.name, tickers,
                )
            else:
                logger.warning(
                    "[WatchlistManager] %s is empty — falling back to WATCHLIST env",
                    self._path.name,
                )
                self._tickers = self._load_from_env()

        except Exception as exc:
            logger.error(
                "[WatchlistManager] Failed to read %s: %s — falling back to env",
                self._path, exc,
            )
            self._tickers = self._load_from_env()

    @staticmethod
    def _load_from_env() -> list[str]:
        raw = os.getenv("WATCHLIST", "")
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if tickers:
            logger.info("[WatchlistManager] Loaded %d ticker(s) from WATCHLIST env", len(tickers))
        else:
            logger.warning("[WatchlistManager] WATCHLIST env is also empty — watchlist is empty")
        return tickers
