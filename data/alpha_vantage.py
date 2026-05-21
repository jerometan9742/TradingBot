"""
Alpha Vantage data connector.

Provides: OHLCV, fundamentals, 50+ technical indicators,
news sentiment, earnings calendar.

Free tier:  25 req/day (enough for prototyping)
Paid tier:  $49.99/mo — 75 req/min, 15-min delayed US data
"""

import os
import requests
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageClient:
    def __init__(self):
        self.api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
        if not self.api_key:
            raise ValueError("ALPHA_VANTAGE_API_KEY not set in .env")

    def _get(self, params: dict) -> dict:
        params["apikey"] = self.api_key
        response = requests.get(BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Alpha Vantage returns errors inside the JSON body
        if "Error Message" in data:
            raise ValueError(f"Alpha Vantage error: {data['Error Message']}")
        if "Note" in data:
            raise RuntimeError(f"Rate limit hit: {data['Note']}")

        return data

    def get_daily_ohlcv(self, ticker: str, outputsize: str = "compact") -> dict:
        """
        Fetch daily OHLCV data.
        outputsize: 'compact' = last 100 days, 'full' = 20+ years
        """
        return self._get({
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": ticker,
            "outputsize": outputsize,
        })

    def get_company_overview(self, ticker: str) -> dict:
        """
        Fetch company fundamentals: P/E, EPS, market cap,
        52-week high/low, dividend yield, sector, description.
        """
        return self._get({
            "function": "OVERVIEW",
            "symbol": ticker,
        })

    def get_income_statement(self, ticker: str) -> dict:
        """Annual and quarterly income statements."""
        return self._get({
            "function": "INCOME_STATEMENT",
            "symbol": ticker,
        })

    def get_balance_sheet(self, ticker: str) -> dict:
        """Annual and quarterly balance sheets."""
        return self._get({
            "function": "BALANCE_SHEET",
            "symbol": ticker,
        })

    def get_cash_flow(self, ticker: str) -> dict:
        """Annual and quarterly cash flow statements."""
        return self._get({
            "function": "CASH_FLOW",
            "symbol": ticker,
        })

    def get_earnings(self, ticker: str) -> dict:
        """Historical EPS actuals vs estimates."""
        return self._get({
            "function": "EARNINGS",
            "symbol": ticker,
        })

    def get_news_sentiment(self, ticker: str, limit: int = 50) -> dict:
        """
        News and sentiment scores for a ticker.
        Returns articles with overall_sentiment_score and label.
        """
        return self._get({
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "limit": limit,
            "sort": "LATEST",
        })

    def get_rsi(self, ticker: str, interval: str = "daily", period: int = 14) -> dict:
        """Relative Strength Index."""
        return self._get({
            "function": "RSI",
            "symbol": ticker,
            "interval": interval,
            "time_period": period,
            "series_type": "close",
        })

    def get_macd(self, ticker: str, interval: str = "daily") -> dict:
        """MACD indicator."""
        return self._get({
            "function": "MACD",
            "symbol": ticker,
            "interval": interval,
            "series_type": "close",
        })

    def get_bollinger_bands(self, ticker: str, interval: str = "daily", period: int = 20) -> dict:
        """Bollinger Bands."""
        return self._get({
            "function": "BBANDS",
            "symbol": ticker,
            "interval": interval,
            "time_period": period,
            "series_type": "close",
        })

    def get_atr(self, ticker: str, interval: str = "daily", period: int = 14) -> dict:
        """Average True Range."""
        return self._get({
            "function": "ATR",
            "symbol": ticker,
            "interval": interval,
            "time_period": period,
        })
