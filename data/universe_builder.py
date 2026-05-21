"""
Universe auto-builder — populates watchlist_universe.txt with fresh candidates.

Runs daily at 18:00 SGT (before the 19:00 screener, before 20:00 pre-market scan).

Sources
-------
A — Market movers via yfinance predefined screeners
    • day_gainers:   today's top % price gainers
    • most_actives:  today's highest trading volume
    Filter: price ≥ $10, market cap ≥ $1B

B — Fundamental quality screen
    • Tries FMP stock-screener first (US, market cap ≥ $10B, avg vol ≥ 1M, price ≥ $10)
    • Falls back to yfinance undervalued_large_caps + growth_technology_stocks
      if FMP screener is not accessible on the current plan

Core list (always included regardless of source results):
    AAPL, MSFT, NVDA, META, GOOGL, AMZN, TSLA, AMD

Universe capped at 50 tickers. Output: watchlist_universe.txt

Usage:
    python data/universe_builder.py          # build and print diff
    from data.universe_builder import UniverseBuilder
    result = UniverseBuilder().build()
"""

import logging
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "watchlist_universe.txt"

CORE_TICKERS: list[str] = [
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN", "TSLA", "AMD",
]

_MAX_UNIVERSE  = 50
_MIN_PRICE_A   = 10.0
_MIN_MCAP_A    = 1_000_000_000      # $1B — Source A threshold
_MIN_PRICE_B   = 10.0
_MIN_MCAP_B    = 10_000_000_000     # $10B — Source B threshold
_MIN_VOLUME_B  = 1_000_000
_SOURCE_A_COUNT = 25               # how many to fetch from each yf screener
_SOURCE_B_COUNT = 30               # how many to fetch from FMP / yf fallback


class UniverseBuilder:
    """
    Builds the screening universe from live market data and writes it to disk.

    Call build() once per day. Returns a diff dict so callers can send a
    Telegram notification describing what changed.
    """

    def __init__(self, universe_path: Path = _UNIVERSE_PATH):
        self._path = Path(universe_path)

    def build(self) -> dict:
        """
        Fetch, filter, merge, and write the new universe.

        Returns:
            {
                "total":   int,
                "added":   list[str],   # tickers new vs previous file
                "removed": list[str],   # tickers dropped vs previous file
                "tickers": list[str],   # complete new list
                "core":    list[str],   # always-protected core tickers
                "sources": dict,        # {"source_a": n, "source_b": n}
            }
        """
        previous = set(self._read())

        source_a = self._fetch_market_movers()
        source_b = self._fetch_fundamental_screen()

        logger.info(
            "[UniverseBuilder] Raw — source_a=%d  source_b=%d",
            len(source_a), len(source_b),
        )

        # Priority merge: core always first, then movers, then quality
        merged = list(dict.fromkeys(CORE_TICKERS + source_a + source_b))
        new_universe = merged[:_MAX_UNIVERSE]

        # Safety: if both sources completely failed, keep previous universe
        if not source_a and not source_b:
            logger.error(
                "[UniverseBuilder] Both sources failed — keeping previous universe"
            )
            return {
                "total":   len(previous),
                "added":   [],
                "removed": [],
                "tickers": sorted(previous),
                "core":    CORE_TICKERS,
                "sources": {"source_a": 0, "source_b": 0},
            }

        self._write(new_universe)

        new_set = set(new_universe)
        added   = sorted(new_set - previous)
        removed = sorted(previous - new_set)

        logger.info(
            "[UniverseBuilder] Done — %d tickers  added=%d  removed=%d",
            len(new_universe), len(added), len(removed),
        )

        return {
            "total":   len(new_universe),
            "added":   added,
            "removed": removed,
            "tickers": new_universe,
            "core":    CORE_TICKERS,
            "sources": {"source_a": len(source_a), "source_b": len(source_b)},
        }

    # ── Source A: market movers ───────────────────────────────────────────────

    def _fetch_market_movers(self) -> list[str]:
        """
        Source A — yfinance day_gainers + most_actives.
        Filter: price ≥ $10, market cap ≥ $1B, plain US ticker (no exchange suffix).
        """
        from yfinance import screen as yf_screen

        tickers: list[str] = []

        for query in ("day_gainers", "most_actives"):
            try:
                result = yf_screen(query, count=_SOURCE_A_COUNT)
                quotes = result.get("quotes", [])
                added_this = 0
                for q in quotes:
                    symbol = q.get("symbol", "")
                    price  = q.get("regularMarketPrice") or 0.0
                    mcap   = q.get("marketCap") or 0.0
                    if (
                        symbol
                        and price >= _MIN_PRICE_A
                        and mcap  >= _MIN_MCAP_A
                        and _is_plain_us_ticker(symbol)
                    ):
                        tickers.append(symbol)
                        added_this += 1
                logger.info(
                    "[UniverseBuilder] Source A (%s): %d qualifying", query, added_this
                )
            except Exception as exc:
                logger.warning("[UniverseBuilder] Source A %s failed: %s", query, exc)

        return list(dict.fromkeys(tickers))

    # ── Source B: fundamental quality screen ─────────────────────────────────

    def _fetch_fundamental_screen(self) -> list[str]:
        """
        Source B — tries FMP screener, falls back to yfinance large-cap screens.
        Filter: price ≥ $10, market cap ≥ $10B, avg daily volume ≥ 1M.
        """
        # Attempt FMP (will gracefully fail on free tier)
        try:
            fmp_tickers = self._fetch_fmp_screener()
            if fmp_tickers:
                logger.info(
                    "[UniverseBuilder] Source B via FMP: %d tickers", len(fmp_tickers)
                )
                return fmp_tickers
        except Exception as exc:
            logger.info(
                "[UniverseBuilder] FMP screener unavailable (%s) — using yfinance fallback",
                exc,
            )

        return self._fetch_yf_large_cap_screen()

    def _fetch_fmp_screener(self) -> list[str]:
        """
        FMP /stable/stock-screener: US large-cap, high-volume stocks.
        Raises RuntimeError if unavailable so caller can fall back.
        """
        api_key = os.getenv("FMP_API_KEY", "")
        if not api_key:
            raise RuntimeError("FMP_API_KEY not configured")

        params = {
            "marketCapMoreThan": _MIN_MCAP_B,
            "volumeMoreThan":    _MIN_VOLUME_B,
            "priceMoreThan":     _MIN_PRICE_B,
            "country":           "US",
            "limit":             _SOURCE_B_COUNT,
            "apikey":            api_key,
        }

        for url in (
            "https://financialmodelingprep.com/stable/stock-screener",
            "https://financialmodelingprep.com/api/v3/stock-screener",
        ):
            try:
                resp = requests.get(url, params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        tickers = [
                            row["symbol"]
                            for row in data
                            if row.get("symbol") and _is_plain_us_ticker(row["symbol"])
                        ]
                        if tickers:
                            return tickers
            except requests.exceptions.RequestException:
                pass

        raise RuntimeError("FMP screener returned no data (plan restriction)")

    def _fetch_yf_large_cap_screen(self) -> list[str]:
        """
        yfinance fallback for Source B.
        Uses undervalued_large_caps + growth_technology_stocks predefined screens.
        """
        from yfinance import screen as yf_screen

        tickers: list[str] = []

        for query in ("undervalued_large_caps", "growth_technology_stocks"):
            try:
                result = yf_screen(query, count=_SOURCE_B_COUNT)
                added_this = 0
                for q in result.get("quotes", []):
                    symbol = q.get("symbol", "")
                    price  = q.get("regularMarketPrice") or 0.0
                    mcap   = q.get("marketCap") or 0.0
                    vol    = q.get("regularMarketVolume") or 0.0
                    if (
                        symbol
                        and price >= _MIN_PRICE_B
                        and mcap  >= _MIN_MCAP_B
                        and vol   >= _MIN_VOLUME_B
                        and _is_plain_us_ticker(symbol)
                    ):
                        tickers.append(symbol)
                        added_this += 1
                logger.info(
                    "[UniverseBuilder] Source B yf (%s): %d qualifying", query, added_this
                )
            except Exception as exc:
                logger.warning(
                    "[UniverseBuilder] Source B yf %s failed: %s", query, exc
                )

        return list(dict.fromkeys(tickers))

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _read(self) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return [
            ln.split("#")[0].strip().upper()
            for ln in lines
            if ln.split("#")[0].strip()
        ]

    def _write(self, tickers: list[str]) -> None:
        header = (
            "# Screening universe — auto-updated daily by data/universe_builder.py\n"
            "# Ranked by data/screener.py at 19:00 SGT → top-15 sent to Telegram\n"
            "# Manual override: /universe add TICKER  or  /universe remove TICKER\n\n"
        )
        self._path.write_text(
            header + "\n".join(tickers) + "\n", encoding="utf-8"
        )
        logger.info(
            "[UniverseBuilder] Wrote %d tickers to %s", len(tickers), self._path.name
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

_EXCHANGE_SUFFIXES = frozenset({
    ".L", ".TO", ".AX", ".HK", ".SI", ".T", ".DE", ".PA", ".AS",
    ".BR", ".MI", ".MC", ".SW", ".ST", ".HE", ".OL", ".NZ",
})

def _is_plain_us_ticker(symbol: str) -> bool:
    """Return True for US-listed tickers. Excludes foreign exchange suffixes."""
    upper = symbol.upper()
    return not any(upper.endswith(s) for s in _EXCHANGE_SUFFIXES)


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    result = UniverseBuilder().build()

    print(f"\n🌐 Universe updated: {result['total']} tickers")
    if result["added"]:
        print(f"  New additions:  {', '.join(result['added'])}")
    if result["removed"]:
        print(f"  Removed:        {', '.join(result['removed'])}")
    print(f"  Core protected: {', '.join(result['core'])}")
    print(f"  Sources: A={result['sources']['source_a']}  B={result['sources']['source_b']}")
    print(f"\nFull universe ({result['total']} tickers):")
    for i, t in enumerate(result["tickers"], 1):
        print(f"  {i:>2}. {t}")
