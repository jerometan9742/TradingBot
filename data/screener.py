"""
Daily momentum screener — ranks universe tickers by composite score (1-10).

Data source: yfinance bulk download (free, no API key, one network call).
No Alpha Vantage requests are consumed — preserves the 25 req/day free tier
for the main trading pipeline.

Metrics and weights:
    5-day price change %        30%
    Volume vs 20-day average    25%
    RSI(14) — calculated here   25%
    Distance from EMA(50)       20%

Usage:
    from data.screener import MomentumScreener
    results = MomentumScreener().screen()   # list[dict], sorted by score desc
"""

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "watchlist_universe.txt"
_MIN_BARS      = 55   # need 55 daily bars for EMA(50) + 5-day lookback
_WEIGHTS       = {"change": 0.30, "volume": 0.25, "rsi": 0.25, "ema": 0.20}


class MomentumScreener:
    """
    Ranks a universe of tickers by composite momentum score.

    All data is fetched in a single yfinance.download() call, keeping
    external API usage near zero.
    """

    def __init__(self, universe_path: Path = _UNIVERSE_PATH):
        self._universe_path = Path(universe_path)

    def load_universe(self) -> list[str]:
        """Read tickers from watchlist_universe.txt."""
        if not self._universe_path.exists():
            logger.error("[Screener] Universe file not found: %s", self._universe_path)
            return []
        lines = self._universe_path.read_text(encoding="utf-8").splitlines()
        tickers = []
        for line in lines:
            t = line.split("#")[0].strip().upper()
            if t:
                tickers.append(t)
        logger.info("[Screener] Loaded %d-ticker universe from %s",
                    len(tickers), self._universe_path.name)
        return tickers

    def screen(
        self,
        tickers: Optional[list] = None,
        top_n: int = 15,
    ) -> list[dict]:
        """
        Screen tickers and return the top_n sorted by score (highest first).

        Args:
            tickers: override the universe file (useful for /screen command)
            top_n:   maximum results to return

        Returns:
            list of {ticker, score, change_5d, volume_ratio, rsi_14,
                     ema50_dist_pct, price}
        """
        if tickers is None:
            tickers = self.load_universe()
        if not tickers:
            logger.warning("[Screener] Empty ticker list — nothing to screen")
            return []

        logger.info("[Screener] Fetching 3-month OHLCV for %d tickers", len(tickers))
        close_df, vol_df = self._download(tickers)
        if close_df is None:
            return []

        results = []
        for ticker in tickers:
            try:
                row = self._compute(ticker, close_df, vol_df)
                if row is not None:
                    results.append(row)
                else:
                    logger.debug("[Screener] Skipped %s — insufficient data", ticker)
            except Exception as exc:
                logger.warning("[Screener] Error scoring %s: %s", ticker, exc)

        results.sort(key=lambda r: r["score"], reverse=True)
        top = results[:top_n]
        logger.info(
            "[Screener] Done — %d/%d tickers scored, top 5: %s",
            len(results),
            len(tickers),
            [(r["ticker"], r["score"]) for r in top[:5]],
        )
        return top

    # ── Data download ─────────────────────────────────────────────────────────

    def _download(self, tickers: list) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """Bulk-download 3 months of daily OHLCV via yfinance."""
        try:
            raw = yf.download(
                tickers,
                period="3mo",
                interval="1d",
                progress=False,
                threads=True,
                auto_adjust=True,
            )
        except Exception as exc:
            logger.error("[Screener] yfinance download failed: %s", exc, exc_info=True)
            return None, None

        # Normalise to (date × ticker) DataFrames regardless of how many tickers
        if isinstance(raw.columns, pd.MultiIndex):
            close_df = raw["Close"]
            vol_df   = raw["Volume"]
        else:
            # Single-ticker download: columns are metric names, not tickers
            single = tickers[0]
            close_df = raw[["Close"]].rename(columns={"Close": single})
            vol_df   = raw[["Volume"]].rename(columns={"Volume": single})

        return close_df, vol_df

    # ── Per-ticker metrics ────────────────────────────────────────────────────

    def _compute(
        self,
        ticker: str,
        close_df: pd.DataFrame,
        vol_df: pd.DataFrame,
    ) -> Optional[dict]:
        if ticker not in close_df.columns:
            return None

        closes  = close_df[ticker].dropna()
        volumes = vol_df[ticker].dropna() if ticker in vol_df.columns else pd.Series(dtype=float)

        if len(closes) < _MIN_BARS:
            return None

        price = float(closes.iloc[-1])

        # 1. 5-day price change
        change_5d = (closes.iloc[-1] / closes.iloc[-6] - 1.0) * 100.0

        # 2. Volume vs 20-day average (compare last day to prior 20-day window)
        if len(volumes) >= 21:
            vol_today  = float(volumes.iloc[-1])
            vol_20_avg = float(volumes.iloc[-21:-1].mean())
            vol_ratio  = vol_today / vol_20_avg if vol_20_avg > 0 else 1.0
        else:
            vol_ratio = 1.0

        # 3. RSI(14)
        rsi_14 = _calc_rsi(closes)

        # 4. EMA(50) distance
        ema50       = float(closes.ewm(span=50, min_periods=50).mean().iloc[-1])
        ema50_dist  = (price - ema50) / ema50 * 100.0 if ema50 > 0 else 0.0

        # Composite score
        score = (
            _WEIGHTS["change"] * _score_change(float(change_5d)) +
            _WEIGHTS["volume"] * _score_volume(vol_ratio) +
            _WEIGHTS["rsi"]    * _score_rsi(rsi_14) +
            _WEIGHTS["ema"]    * _score_ema(ema50_dist)
        )

        return {
            "ticker":         ticker,
            "score":          round(score, 2),
            "change_5d":      round(float(change_5d), 2),
            "volume_ratio":   round(vol_ratio, 2),
            "rsi_14":         round(rsi_14, 1),
            "ema50_dist_pct": round(ema50_dist, 2),
            "price":          round(price, 2),
        }


# ── Standalone scoring functions (0–10) ──────────────────────────────────────

def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    """Wilder's smoothed RSI."""
    delta    = closes.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(avg_gain.iloc[-1]) / last_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _score_change(pct: float) -> float:
    """Linear: -10% → 0, 0% → 5, +10% → 10."""
    return max(0.0, min(10.0, (pct + 10.0) / 2.0))


def _score_volume(ratio: float) -> float:
    """0.5x → 0, 1x → 3, 2x → 7, 3x+ → 10."""
    if ratio <= 0.5:
        return 0.0
    if ratio <= 1.0:
        return (ratio - 0.5) / 0.5 * 3.0
    if ratio <= 2.0:
        return 3.0 + (ratio - 1.0) * 4.0
    return min(10.0, 7.0 + (ratio - 2.0) * 3.0)


def _score_rsi(rsi: float) -> float:
    """
    Momentum sweet spot: 55–70 scores highest (building/in-trend, not overbought).
    Overbought (>80) and deeply oversold (<30) score lower.
    """
    if rsi < 30:
        return 3.0
    if rsi < 40:
        return 3.0 + (rsi - 30) * 0.2   # 3 → 5
    if rsi < 50:
        return 5.0 + (rsi - 40) * 0.2   # 5 → 7
    if rsi < 60:
        return 7.0 + (rsi - 50) * 0.2   # 7 → 9
    if rsi < 70:
        return 9.0 - (rsi - 60) * 0.1   # 9 → 8
    if rsi < 80:
        return 8.0 - (rsi - 70) * 0.5   # 8 → 3
    return max(0.0, 3.0 - (rsi - 80) * 0.3)


def _score_ema(dist_pct: float) -> float:
    """
    Slightly above EMA50 scores highest; far-extended or below EMA scores lower.
    Best zone: 2–5% above EMA50 (confirmed uptrend, not overextended).
    """
    if dist_pct < -15:
        return 1.0
    if dist_pct < -5:
        return 1.0 + (dist_pct + 15) / 10.0 * 3.0   # 1 → 4
    if dist_pct < 0:
        return 4.0 + (dist_pct + 5) / 5.0 * 3.0     # 4 → 7
    if dist_pct < 5:
        return 7.0 + dist_pct / 5.0 * 3.0            # 7 → 10
    if dist_pct < 10:
        return 10.0 - (dist_pct - 5) / 5.0 * 2.0    # 10 → 8
    if dist_pct < 20:
        return 8.0 - (dist_pct - 10) / 10.0 * 4.0   # 8 → 4
    return max(1.0, 4.0 - (dist_pct - 20) / 10.0 * 3.0)


# ── CLI entry point ───────────────────────────────────────────────────────────

def _print_results(results: list) -> None:
    if not results:
        print("No results — check data download or universe file.")
        return
    print(f"\n{'🔍 Daily Screen — Top ' + str(len(results)) + ' Candidates':}")
    print("Sorted by momentum score:\n")
    for i, r in enumerate(results, 1):
        arrow = "▲" if r["change_5d"] >= 0 else "▼"
        print(
            f"{i:>2}. {r['ticker']:<6} {arrow} {r['score']:.1f}/10  "
            f"RSI:{r['rsi_14']:.0f}  Vol:{r['volume_ratio']:.1f}x avg  "
            f"5d:{r['change_5d']:+.1f}%  EMA50:{r['ema50_dist_pct']:+.1f}%  "
            f"${r['price']:.2f}"
        )
    print()


if __name__ == "__main__":
    import argparse
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Daily momentum screener")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one screen cycle and print results",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Number of top tickers to show (default: 15)",
    )
    args = parser.parse_args()

    results = MomentumScreener().screen(top_n=args.top)
    _print_results(results)
