"""
Financial Modeling Prep (FMP) data connector.

Provides: all-in-one fundamentals, DCF valuations, ratios,
real-time quotes, WebSocket streaming.

Pricing: $19/mo starter — unlimited REST + WebSocket, no request caps.
Best for: comprehensive fundamental data in one API.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://financialmodelingprep.com/api/v3"


class FMPClient:
    def __init__(self):
        self.api_key = os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY not set in .env")

    def _get(self, endpoint: str, params: dict = None) -> "dict | list":
        if params is None:
            params = {}
        params["apikey"] = self.api_key
        url = f"{BASE_URL}/{endpoint}"
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def get_income_statement(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """Revenue, gross profit, EBITDA, net income — last N periods."""
        return self._get(f"income-statement/{ticker}", {"period": period, "limit": limit})

    def get_balance_sheet(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """Assets, liabilities, equity, cash — last N periods."""
        return self._get(f"balance-sheet-statement/{ticker}", {"period": period, "limit": limit})

    def get_cash_flow(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """Operating, investing, financing cash flows — last N periods."""
        return self._get(f"cash-flow-statement/{ticker}", {"period": period, "limit": limit})

    def get_key_ratios(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """
        P/E, P/B, P/S, EV/EBITDA, ROE, ROA, debt/equity,
        current ratio, dividend yield — last N periods.
        """
        return self._get(f"ratios/{ticker}", {"period": period, "limit": limit})

    def get_dcf(self, ticker: str) -> dict:
        """Intrinsic value via Discounted Cash Flow model."""
        result = self._get(f"discounted-cash-flow/{ticker}")
        return result[0] if result else {}

    def get_company_profile(self, ticker: str) -> dict:
        """Name, sector, industry, country, market cap, description, CEO."""
        result = self._get(f"profile/{ticker}")
        return result[0] if result else {}

    def get_quote(self, ticker: str) -> dict:
        """Real-time price, volume, market cap, 52-week range, P/E."""
        result = self._get(f"quote/{ticker}")
        return result[0] if result else {}

    def get_analyst_estimates(self, ticker: str, period: str = "annual") -> list:
        """Consensus EPS and revenue estimates from analysts."""
        return self._get(f"analyst-estimates/{ticker}", {"period": period})

    def get_price_target(self, ticker: str) -> list:
        """Analyst price targets with individual firm breakdowns."""
        return self._get(f"price-target/{ticker}")

    def get_earnings_surprises(self, ticker: str, limit: int = 8) -> list:
        """Historical EPS actuals vs estimates with surprise %."""
        return self._get(f"earnings-surprises/{ticker}", {"limit": limit})

    def get_peers(self, ticker: str) -> list:
        """Comparable companies in same sector."""
        return self._get(f"stock_peers", {"symbol": ticker})
