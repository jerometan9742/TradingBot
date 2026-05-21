"""
Unified DataFetcher — normalises data from all sources into
a single structured dict that feeds into the TradingAgents pipeline.

Usage:
    fetcher = DataFetcher()
    data = fetcher.fetch("AAPL")
    # Returns a clean dict with fundamentals, technicals, news, sentiment

Data sources:
    FMP             → fundamentals (US stocks primary)
    Finnhub         → real-time quote, earnings calendar, news (US)
    AlphaVantage    → OHLCV history, technical indicators
    yfinance        → primary source for intl tickers (.SI/.HK/.L etc);
                      also gap-fills FMP fundamentals for US stocks
    NewsAPI         → additional news articles (100 req/day free)
    FearGreedClient → CNN Fear & Greed Index (free, no key)
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

from data.alpha_vantage import AlphaVantageClient
from data.fear_greed import FearGreedClient
from data.finnhub import FinnhubClient
from data.fmp import FMPClient
from data.newsapi import NewsAPIConnector
from data.yfinance_connector import YFinanceClient

load_dotenv()

logger = logging.getLogger(__name__)

# Ticker suffixes that indicate non-US listings — yfinance is primary for these
_INTL_SUFFIXES = {".SI", ".HK", ".L", ".T", ".AX", ".TO", ".NS", ".BO", ".KL", ".NZ"}


class DataFetcher:
    """
    Fetches and normalises financial data from multiple sources.

    Routing:
        US tickers  → FMP fundamentals + Finnhub quote/news, yfinance gap-fill
        Intl tickers → yfinance primary for quote + fundamentals
        All tickers  → AlphaVantage technicals, NewsAPI + yfinance news,
                        CNN Fear & Greed sentiment
    """

    def __init__(self):
        self.av = AlphaVantageClient()
        self.fh = FinnhubClient()
        self.fmp = FMPClient()
        self.yfin = YFinanceClient()
        self.fg = FearGreedClient()

        # NewsAPI is optional — skip gracefully if key is absent
        try:
            self.newsapi = NewsAPIConnector()
        except ValueError:
            self.newsapi = None
            logger.warning("[DataFetcher] NEWSAPI_KEY not set — NewsAPI disabled")

    def _is_intl(self, ticker: str) -> bool:
        """Return True for non-US tickers that yfinance handles better."""
        upper = ticker.upper()
        return any(upper.endswith(s) for s in _INTL_SUFFIXES)

    def fetch(self, ticker: str) -> dict:
        """
        Main entry point. Returns a unified data dict for one ticker.

        Returns:
            {
                "ticker":      str,
                "fetched_at":  ISO timestamp,
                "quote":       { price, change_pct, volume, market_cap, ... },
                "fundamentals":{ company_name, sector, pe_ratio, revenue, ... },
                "technicals":  { rsi_14, macd, bollinger_bands },
                "sentiment":   { score, label, article_count,
                                 fear_greed_score, fear_greed_rating },
                "news":        [ { headline, source, datetime, url, summary } ],
                "earnings":    { next_date, last_surprise_pct },
                "analyst":     { buy, hold, sell, price_target_mean },
                "fear_greed":  { score, rating, previous_close, ... },
            }
        """
        logger.info("[DataFetcher] Fetching %s (intl=%s)", ticker, self._is_intl(ticker))

        result: dict = {
            "ticker": ticker,
            "fetched_at": datetime.utcnow().isoformat(),
        }

        result["quote"] = self._fetch_quote(ticker)
        result["fundamentals"] = self._fetch_fundamentals(ticker)
        result["technicals"] = self._fetch_technicals(ticker)
        result["fear_greed"] = self._fetch_fear_greed()
        result["sentiment"] = self._fetch_sentiment(ticker, result["fear_greed"])

        company_name = result["fundamentals"].get("company_name", "")
        result["news"] = self._fetch_news(ticker, company_name=company_name)

        result["earnings"] = self._fetch_earnings(ticker)
        result["analyst"] = self._fetch_analyst(ticker)

        logger.info("[DataFetcher] Done: %s", ticker)
        return result

    # ── Quote ─────────────────────────────────────────────────────────────────

    def _fetch_quote(self, ticker: str) -> dict:
        if self._is_intl(ticker):
            try:
                return self.yfin.get_quote(ticker)
            except Exception as e:
                logger.error("[DataFetcher] yfinance quote error (%s): %s", ticker, e)
                return {}

        try:
            q = self.fh.get_quote(ticker)
            return {
                "price": q.get("c"),
                "change_pct": q.get("dp"),
                "high": q.get("h"),
                "low": q.get("l"),
                "open": q.get("o"),
                "prev_close": q.get("pc"),
                "volume": None,  # not in Finnhub free quote
            }
        except Exception as e:
            logger.error("[DataFetcher] Quote error (%s): %s", ticker, e)
            return {}

    # ── Fundamentals ──────────────────────────────────────────────────────────

    def _fetch_fundamentals(self, ticker: str) -> dict:
        if self._is_intl(ticker):
            try:
                return self.yfin.get_fundamentals(ticker)
            except Exception as e:
                logger.error("[DataFetcher] yfinance fundamentals error (%s): %s", ticker, e)
                return {}

        # US path: FMP primary, yfinance gap-fill
        fmp_data: dict = {}
        try:
            profile = self.fmp.get_company_profile(ticker)
            ratios = self.fmp.get_key_ratios(ticker, limit=1)
            income = self.fmp.get_income_statement(ticker, limit=1)
            latest_ratios = ratios[0] if ratios else {}
            latest_income = income[0] if income else {}
            fmp_data = {
                "company_name": profile.get("companyName"),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "market_cap": profile.get("marketCap"),
                "description": (profile.get("description") or "")[:500],
                "pe_ratio": latest_ratios.get("priceToEarningsRatio"),
                "pb_ratio": latest_ratios.get("priceToBookRatio"),
                "ev_ebitda": latest_ratios.get("enterpriseValueMultiple"),
                "roe": latest_ratios.get("returnOnEquity"),
                "debt_equity": latest_ratios.get("debtToEquityRatio"),
                "revenue": latest_income.get("revenue"),
                "net_income": latest_income.get("netIncome"),
                "gross_margin": latest_ratios.get("grossProfitMargin"),
                "operating_margin": latest_ratios.get("operatingProfitMargin"),
            }
        except Exception as e:
            logger.error("[DataFetcher] FMP fundamentals error (%s): %s", ticker, e)

        # Gap-fill any None values from yfinance
        try:
            yf_data = self.yfin.get_fundamentals(ticker)
            for key, val in yf_data.items():
                if fmp_data.get(key) is None and val is not None:
                    fmp_data[key] = val
        except Exception as e:
            logger.warning("[DataFetcher] yfinance gap-fill error (%s): %s", ticker, e)

        return fmp_data

    # ── Technicals ────────────────────────────────────────────────────────────

    def _fetch_technicals(self, ticker: str) -> dict:
        try:
            rsi_data = self.av.get_rsi(ticker)
            macd_data = self.av.get_macd(ticker)
            bb_data = self.av.get_bollinger_bands(ticker)

            rsi_series = rsi_data.get("Technical Analysis: RSI", {})
            macd_series = macd_data.get("Technical Analysis: MACD", {})
            bb_series = bb_data.get("Technical Analysis: BBANDS", {})

            latest_rsi_date = next(iter(rsi_series), None)
            latest_macd_date = next(iter(macd_series), None)
            latest_bb_date = next(iter(bb_series), None)

            return {
                "rsi_14": float(rsi_series[latest_rsi_date]["RSI"]) if latest_rsi_date else None,
                "macd": float(macd_series[latest_macd_date]["MACD"]) if latest_macd_date else None,
                "macd_signal": float(macd_series[latest_macd_date]["MACD_Signal"]) if latest_macd_date else None,
                "macd_hist": float(macd_series[latest_macd_date]["MACD_Hist"]) if latest_macd_date else None,
                "bb_upper": float(bb_series[latest_bb_date]["Real Upper Band"]) if latest_bb_date else None,
                "bb_middle": float(bb_series[latest_bb_date]["Real Middle Band"]) if latest_bb_date else None,
                "bb_lower": float(bb_series[latest_bb_date]["Real Lower Band"]) if latest_bb_date else None,
            }
        except Exception as e:
            logger.error("[DataFetcher] Technicals error (%s): %s", ticker, e)
            return {}

    # ── Fear & Greed ──────────────────────────────────────────────────────────

    def _fetch_fear_greed(self) -> dict:
        try:
            return self.fg.get()
        except Exception as e:
            logger.error("[DataFetcher] Fear & Greed error: %s", e)
            return {}

    # ── Sentiment ─────────────────────────────────────────────────────────────

    def _fetch_sentiment(self, ticker: str, fear_greed: dict) -> dict:
        result: dict = {}

        try:
            sentiment = self.fh.get_news_sentiment(ticker)
            result.update({
                "score": sentiment.get("sentiment", {}).get("bullishPercent"),
                "bearish_pct": sentiment.get("sentiment", {}).get("bearishPercent"),
                "article_count": sentiment.get("buzz", {}).get("articlesInLastWeek"),
                "weekly_average": sentiment.get("buzz", {}).get("weeklyAverage"),
            })
        except Exception as e:
            logger.warning("[DataFetcher] Finnhub sentiment error (%s): %s", ticker, e)

        # Attach CNN Fear & Greed to the sentiment block as well
        result["fear_greed_score"] = fear_greed.get("score")
        result["fear_greed_rating"] = fear_greed.get("rating")

        return result

    # ── News ──────────────────────────────────────────────────────────────────

    def _fetch_news(self, ticker: str, company_name: str = "", days: int = 7) -> list:
        """
        Merge news from Finnhub + NewsAPI + yfinance, deduplicated by headline.
        Returns up to 15 most recent articles.
        """
        articles: list = []

        # Finnhub
        try:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")
            from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            raw = self.fh.get_company_news(ticker, from_date, to_date)
            for a in raw[:10]:
                articles.append({
                    "headline": a.get("headline"),
                    "source": a.get("source"),
                    "datetime": a.get("datetime"),
                    "url": a.get("url"),
                    "summary": (a.get("summary") or "")[:300],
                })
        except Exception as e:
            logger.warning("[DataFetcher] Finnhub news error (%s): %s", ticker, e)

        # NewsAPI
        if self.newsapi:
            try:
                articles.extend(
                    self.newsapi.get_articles(ticker, company_name=company_name, page_size=10)
                )
            except Exception as e:
                logger.warning("[DataFetcher] NewsAPI error (%s): %s", ticker, e)

        # yfinance
        try:
            articles.extend(self.yfin.get_news(ticker, limit=10))
        except Exception as e:
            logger.warning("[DataFetcher] yfinance news error (%s): %s", ticker, e)

        # Deduplicate by normalised headline (case-insensitive, strip punctuation)
        seen: set = set()
        unique: list = []
        for a in articles:
            headline = a.get("headline") or ""
            key = headline.lower().strip(" .,!?")
            if key and key not in seen:
                seen.add(key)
                unique.append(a)

        # Sort by datetime descending where possible, cap at 15
        def _ts(a: dict):
            dt = a.get("datetime")
            if isinstance(dt, (int, float)):
                return dt
            if isinstance(dt, str):
                try:
                    return datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return 0
            return 0

        unique.sort(key=_ts, reverse=True)
        return unique[:15]

    # ── Earnings ──────────────────────────────────────────────────────────────

    def _fetch_earnings(self, ticker: str) -> dict:
        if self._is_intl(ticker):
            return {}  # FMP earnings only covers US/major exchanges

        try:
            surprises = self.fmp.get_earnings_surprises(ticker, limit=4)
            latest = surprises[0] if surprises else {}
            actual = latest.get("epsActual")
            estimated = latest.get("epsEstimated")
            return {
                "last_actual_eps": actual,
                "last_estimated_eps": estimated,
                "last_surprise_pct": (
                    ((actual - estimated) / abs(estimated)) * 100
                    if (actual is not None and estimated) else None
                ),
                "last_earnings_date": latest.get("date"),
                "history": surprises,
            }
        except Exception as e:
            logger.error("[DataFetcher] Earnings error (%s): %s", ticker, e)
            return {}

    # ── Analyst ───────────────────────────────────────────────────────────────

    def _fetch_analyst(self, ticker: str) -> dict:
        if self._is_intl(ticker):
            return {}  # Finnhub analyst data is US-focused

        try:
            trends = self.fh.get_recommendation_trends(ticker)
            target = self.fh.get_price_target(ticker)
            latest = trends[0] if trends else {}
            return {
                "buy": latest.get("buy"),
                "hold": latest.get("hold"),
                "sell": latest.get("sell"),
                "strong_buy": latest.get("strongBuy"),
                "strong_sell": latest.get("strongSell"),
                "price_target_mean": target.get("targetMean"),
                "price_target_high": target.get("targetHigh"),
                "price_target_low": target.get("targetLow"),
            }
        except Exception as e:
            logger.error("[DataFetcher] Analyst error (%s): %s", ticker, e)
            return {}
