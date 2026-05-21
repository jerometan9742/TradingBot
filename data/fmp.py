"""
Financial Modeling Prep (FMP) data connector.

Provides: all-in-one fundamentals, DCF valuations, ratios,
real-time quotes, WebSocket streaming.

Pricing: $19/mo starter — unlimited REST + WebSocket, no request caps.
Best for: comprehensive fundamental data in one API.

API note: FMP deprecated v3/v4 endpoints for new subscribers.
All calls now use the /stable/ base URL introduced in 2025.
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# FMP deprecated v3/v4 for new subscribers; /stable/ is the current API.
_BASE = "https://financialmodelingprep.com/stable"


class FMPClient:
    def __init__(self):
        self.api_key = os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY not set in .env")

    def _get(self, endpoint: str, params: dict = None) -> list:
        """GET /stable/{endpoint} and return the parsed JSON (always a list)."""
        if params is None:
            params = {}
        params["apikey"] = self.api_key
        url = f"{_BASE}/{endpoint}"
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else [data]

    def get_income_statement(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """Revenue, gross profit, EBITDA, net income — last N periods."""
        return self._get("income-statement", {"symbol": ticker, "period": period, "limit": limit})

    def get_balance_sheet(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """Assets, liabilities, equity, cash — last N periods."""
        return self._get("balance-sheet-statement", {"symbol": ticker, "period": period, "limit": limit})

    def get_cash_flow(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """Operating, investing, financing cash flows — last N periods."""
        return self._get("cash-flow-statement", {"symbol": ticker, "period": period, "limit": limit})

    def get_key_ratios(self, ticker: str, period: str = "annual", limit: int = 5) -> list:
        """
        P/E, P/B, P/S, EV/EBITDA, debt/equity, margins — last N periods.

        Stable API field names (changed from v3):
            priceToEarningsRatio  (was priceEarningsRatio)
            debtToEquityRatio     (was debtEquityRatio)
            grossProfitMargin     (was in income statement as grossProfitRatio)
            operatingProfitMargin (was in income statement as operatingIncomeRatio)
        """
        return self._get("ratios", {"symbol": ticker, "period": period, "limit": limit})

    def get_dcf(self, ticker: str) -> dict:
        """Intrinsic value via Discounted Cash Flow model."""
        result = self._get("discounted-cash-flow", {"symbol": ticker})
        return result[0] if result else {}

    def get_company_profile(self, ticker: str) -> dict:
        """Name, sector, industry, country, market cap, description, CEO.

        Stable API field name change: marketCap (was mktCap in v3).
        """
        result = self._get("profile", {"symbol": ticker})
        return result[0] if result else {}

    def get_quote(self, ticker: str) -> dict:
        """Real-time price, volume, market cap, 52-week range, P/E."""
        result = self._get("quote", {"symbol": ticker})
        return result[0] if result else {}

    def get_analyst_estimates(self, ticker: str, period: str = "annual") -> list:
        """Consensus EPS and revenue estimates from analysts.

        period='quarter' requires a premium subscription; defaults to 'annual'.
        """
        return self._get("analyst-estimates", {"symbol": ticker, "period": period})

    def get_price_target(self, ticker: str) -> dict:
        """Aggregated analyst price target summary (avg, count by window).

        Stable endpoint is price-target-summary (was price-target in v3).
        Returns a single summary dict rather than a list of individual targets.
        """
        result = self._get("price-target-summary", {"symbol": ticker})
        return result[0] if result else {}

    def get_earnings_surprises(self, ticker: str, limit: int = 8) -> list:
        """Historical EPS actuals vs estimates with surprise calculation.

        Stable endpoint is /stable/earnings (was earnings-surprises in v3).
        Field names changed:
            epsActual    (was actualEarningResult)
            epsEstimated (was estimatedEarning)
        Records with null epsActual are future estimates — filtered out here.
        """
        rows = self._get("earnings", {"symbol": ticker, "limit": limit + 4})
        # Drop future earnings (no actual yet) and cap at requested limit
        historical = [r for r in rows if r.get("epsActual") is not None]
        return historical[:limit]

    def get_peers(self, ticker: str) -> list:
        """Comparable companies in the same sector.

        Note: stock_peers is not available on the /stable/ API as of 2025.
        Returns an empty list so callers degrade gracefully.
        """
        logger.debug("[FMPClient] get_peers: stock_peers unavailable on stable API")
        return []
