"""
yfinance connector — completely free, no API key required.

Works for US stocks and international tickers (DBS.SI, HSBA.L, 7203.T, etc).
Primary data source for SGX / HK / London / ASX stocks that fail on US APIs.
"""

import logging

import yfinance as yf

logger = logging.getLogger(__name__)


class YFinanceClient:
    def get_quote(self, ticker: str) -> dict:
        """Real-time price and volume via fast_info (single network call)."""
        t = yf.Ticker(ticker)
        fi = t.fast_info
        prev = fi.regular_market_previous_close
        price = fi.last_price
        return {
            "price": price,
            "change_pct": (
                (price - prev) / prev * 100 if price and prev else None
            ),
            "high": fi.day_high,
            "low": fi.day_low,
            "open": fi.open,
            "prev_close": prev,
            "volume": fi.last_volume,
            "market_cap": fi.market_cap,
        }

    def get_fundamentals(self, ticker: str) -> dict:
        """
        Key fundamentals: valuation, margins, growth, analyst targets.
        Falls back gracefully if any field is absent.
        """
        t = yf.Ticker(ticker)
        info = t.info
        fi = t.fast_info
        return {
            "company_name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "market_cap": fi.market_cap,
            "description": (info.get("longBusinessSummary") or "")[:500],
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "pb_ratio": info.get("priceToBook"),
            "ev_ebitda": info.get("enterpriseToEbitda"),
            "roe": info.get("returnOnEquity"),
            "debt_equity": info.get("debtToEquity"),
            "revenue": info.get("totalRevenue"),
            "net_income": info.get("netIncomeToCommon"),
            "gross_margin": info.get("grossMargins"),
            "operating_margin": info.get("operatingMargins"),
            "profit_margin": info.get("profitMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "analyst_target_mean": info.get("targetMeanPrice"),
            "analyst_target_high": info.get("targetHighPrice"),
            "analyst_target_low": info.get("targetLowPrice"),
        }

    def get_news(self, ticker: str, limit: int = 10) -> list:
        """Recent news articles from Yahoo Finance (normalised to fetcher schema)."""
        t = yf.Ticker(ticker)
        articles = t.news or []
        result = []
        for a in articles[:limit]:
            content = a.get("content", {})
            provider = content.get("provider") or {}
            canonical = content.get("canonicalUrl") or {}
            headline = content.get("title")
            if not headline:
                continue
            result.append({
                "headline": headline,
                "source": provider.get("displayName") or provider.get("url"),
                "datetime": content.get("pubDate"),
                "url": canonical.get("url"),
                "summary": (content.get("summary") or "")[:300],
            })
        return result
