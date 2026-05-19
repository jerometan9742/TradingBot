"""
Finnhub data connector.

Provides: real-time quotes, earnings calendar, company news,
sentiment scores, SEC filings, ESG scores.

Free tier:  60 req/min — very generous, covers most needs
Paid tier:  $12-50/mo for international stocks and higher limits

Note: International stocks (SGX, HKEX) require a paid plan.
"""

import os
import finnhub
from dotenv import load_dotenv

load_dotenv()


class FinnhubClient:
    def __init__(self):
        api_key = os.getenv("FINNHUB_API_KEY")
        if not api_key:
            raise ValueError("FINNHUB_API_KEY not set in .env")
        self.client = finnhub.Client(api_key=api_key)

    def get_quote(self, ticker: str) -> dict:
        """
        Real-time quote: current price, open, high, low,
        previous close, percent change.
        """
        return self.client.quote(ticker)

    def get_company_profile(self, ticker: str) -> dict:
        """Company name, country, currency, exchange, industry, sector."""
        return self.client.company_profile2(symbol=ticker)

    def get_financials(self, ticker: str, statement: str = "ic", freq: str = "annual") -> dict:
        """
        Financial statements.
        statement: 'ic' (income), 'bs' (balance sheet), 'cf' (cash flow)
        freq: 'annual' or 'quarterly'
        """
        return self.client.financials(ticker, statement, freq)

    def get_basic_financials(self, ticker: str) -> dict:
        """
        Key metrics: P/E, P/S, P/B, EV/EBITDA, beta, 52-week range,
        ROE, ROA, current ratio, debt/equity.
        """
        return self.client.company_basic_financials(ticker, "all")

    def get_earnings_calendar(self, ticker: str) -> dict:
        """Upcoming and past earnings dates with EPS estimates vs actuals."""
        return self.client.earnings_calendar(symbol=ticker, from_="2024-01-01", to="2026-12-31")

    def get_company_news(self, ticker: str, from_date: str, to_date: str) -> list:
        """
        Recent news articles for a ticker.
        from_date / to_date: 'YYYY-MM-DD'
        """
        return self.client.company_news(ticker, _from=from_date, to=to_date)

    def get_news_sentiment(self, ticker: str) -> dict:
        """
        Aggregated news sentiment score (-1 to 1) and
        buzz metrics (article count, weekly mentions).
        """
        return self.client.news_sentiment(ticker)

    def get_recommendation_trends(self, ticker: str) -> list:
        """Analyst buy/hold/sell counts by month."""
        return self.client.recommendation_trends(ticker)

    def get_price_target(self, ticker: str) -> dict:
        """Analyst consensus price target: high, low, mean, median."""
        return self.client.price_target(ticker)

    def get_peers(self, ticker: str) -> list:
        """List of peer companies in the same sector."""
        return self.client.company_peers(ticker)
