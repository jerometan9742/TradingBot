"""
Unified DataFetcher — normalises data from all sources into
a single structured dict that feeds into the TradingAgents pipeline.

Usage:
    fetcher = DataFetcher()
    data = fetcher.fetch("AAPL")
    # Returns a clean dict with fundamentals, technicals, news, sentiment
"""

import os
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

from data.alpha_vantage import AlphaVantageClient
from data.finnhub import FinnhubClient
from data.fmp import FMPClient

load_dotenv()


class DataFetcher:
    """
    Fetches and normalises financial data from multiple sources.

    Data flow:
        FMP         → fundamentals (income, balance sheet, ratios)
        Finnhub     → real-time quote, news, sentiment, earnings calendar
        AlphaVantage → OHLCV history, technical indicators, news sentiment

    Falls back gracefully if any source is unavailable.
    """

    def __init__(self):
        self.av = AlphaVantageClient()
        self.fh = FinnhubClient()
        self.fmp = FMPClient()

    def fetch(self, ticker: str) -> dict:
        """
        Main entry point. Returns a unified data dict for one ticker.

        Args:
            ticker: Stock ticker symbol (e.g. 'AAPL', 'DBS.SI')

        Returns:
            {
                "ticker": str,
                "fetched_at": ISO timestamp,
                "quote": { price, change_pct, volume, market_cap },
                "fundamentals": { revenue, net_income, eps, pe_ratio, ... },
                "technicals": { rsi, macd, bollinger_bands },
                "sentiment": { score, label, article_count },
                "news": [ { headline, source, datetime, sentiment } ],
                "earnings": { next_date, last_surprise_pct },
                "analyst": { buy, hold, sell, price_target_mean },
            }
        """
        print(f"[DataFetcher] Fetching data for {ticker}...")

        result = {
            "ticker": ticker,
            "fetched_at": datetime.utcnow().isoformat(),
        }

        result["quote"] = self._fetch_quote(ticker)
        result["fundamentals"] = self._fetch_fundamentals(ticker)
        result["technicals"] = self._fetch_technicals(ticker)
        result["sentiment"] = self._fetch_sentiment(ticker)
        result["news"] = self._fetch_news(ticker)
        result["earnings"] = self._fetch_earnings(ticker)
        result["analyst"] = self._fetch_analyst(ticker)

        print(f"[DataFetcher] Done: {ticker}")
        return result

    def _fetch_quote(self, ticker: str) -> dict:
        try:
            q = self.fh.get_quote(ticker)
            return {
                "price": q.get("c"),
                "change_pct": q.get("dp"),
                "high": q.get("h"),
                "low": q.get("l"),
                "open": q.get("o"),
                "prev_close": q.get("pc"),
                "volume": None,  # Not in Finnhub free quote
            }
        except Exception as e:
            print(f"[DataFetcher] Quote error ({ticker}): {e}")
            return {}

    def _fetch_fundamentals(self, ticker: str) -> dict:
        try:
            profile = self.fmp.get_company_profile(ticker)
            ratios = self.fmp.get_key_ratios(ticker, limit=1)
            income = self.fmp.get_income_statement(ticker, limit=1)
            latest_ratios = ratios[0] if ratios else {}
            latest_income = income[0] if income else {}
            return {
                "company_name": profile.get("companyName"),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "market_cap": profile.get("mktCap"),
                "description": profile.get("description", "")[:500],
                "pe_ratio": latest_ratios.get("priceEarningsRatio"),
                "pb_ratio": latest_ratios.get("priceToBookRatio"),
                "ev_ebitda": latest_ratios.get("enterpriseValueMultiple"),
                "roe": latest_ratios.get("returnOnEquity"),
                "debt_equity": latest_ratios.get("debtEquityRatio"),
                "revenue": latest_income.get("revenue"),
                "net_income": latest_income.get("netIncome"),
                "gross_margin": latest_income.get("grossProfitRatio"),
                "operating_margin": latest_income.get("operatingIncomeRatio"),
            }
        except Exception as e:
            print(f"[DataFetcher] Fundamentals error ({ticker}): {e}")
            return {}

    def _fetch_technicals(self, ticker: str) -> dict:
        try:
            rsi_data = self.av.get_rsi(ticker)
            macd_data = self.av.get_macd(ticker)
            bb_data = self.av.get_bollinger_bands(ticker)

            # Extract most recent values
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
            print(f"[DataFetcher] Technicals error ({ticker}): {e}")
            return {}

    def _fetch_sentiment(self, ticker: str) -> dict:
        try:
            sentiment = self.fh.get_news_sentiment(ticker)
            return {
                "score": sentiment.get("sentiment", {}).get("bullishPercent"),
                "bearish_pct": sentiment.get("sentiment", {}).get("bearishPercent"),
                "article_count": sentiment.get("buzz", {}).get("articlesInLastWeek"),
                "weekly_average": sentiment.get("buzz", {}).get("weeklyAverage"),
            }
        except Exception as e:
            print(f"[DataFetcher] Sentiment error ({ticker}): {e}")
            return {}

    def _fetch_news(self, ticker: str, days: int = 7) -> list:
        try:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")
            from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            articles = self.fh.get_company_news(ticker, from_date, to_date)
            return [
                {
                    "headline": a.get("headline"),
                    "source": a.get("source"),
                    "datetime": a.get("datetime"),
                    "url": a.get("url"),
                    "summary": a.get("summary", "")[:300],
                }
                for a in articles[:10]  # Cap at 10 most recent
            ]
        except Exception as e:
            print(f"[DataFetcher] News error ({ticker}): {e}")
            return []

    def _fetch_earnings(self, ticker: str) -> dict:
        try:
            surprises = self.fmp.get_earnings_surprises(ticker, limit=4)
            latest = surprises[0] if surprises else {}
            return {
                "last_actual_eps": latest.get("actualEarningResult"),
                "last_estimated_eps": latest.get("estimatedEarning"),
                "last_surprise_pct": (
                    ((latest.get("actualEarningResult", 0) - latest.get("estimatedEarning", 0))
                     / abs(latest.get("estimatedEarning", 1))) * 100
                    if latest.get("estimatedEarning") else None
                ),
                "last_earnings_date": latest.get("date"),
                "history": surprises,
            }
        except Exception as e:
            print(f"[DataFetcher] Earnings error ({ticker}): {e}")
            return {}

    def _fetch_analyst(self, ticker: str) -> dict:
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
            print(f"[DataFetcher] Analyst error ({ticker}): {e}")
            return {}
