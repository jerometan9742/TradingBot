"""
Unit tests for DataFetcher.
Run with: pytest tests/test_data_fetcher.py -v
"""

import pytest
from unittest.mock import patch, MagicMock
from data.fetcher import DataFetcher


@pytest.fixture
def mock_fetcher():
    """Return a DataFetcher with all API clients mocked."""
    with patch("data.fetcher.AlphaVantageClient"), \
         patch("data.fetcher.FinnhubClient"), \
         patch("data.fetcher.FMPClient"):
        fetcher = DataFetcher()
        yield fetcher


def test_fetch_returns_required_keys(mock_fetcher):
    """fetch() must always return these top-level keys."""
    mock_fetcher.fh.get_quote.return_value = {"c": 150.0, "dp": 1.5}
    mock_fetcher.fmp.get_company_profile.return_value = {"companyName": "Apple"}
    mock_fetcher.fmp.get_key_ratios.return_value = []
    mock_fetcher.fmp.get_income_statement.return_value = []
    mock_fetcher.av.get_rsi.return_value = {}
    mock_fetcher.av.get_macd.return_value = {}
    mock_fetcher.av.get_bollinger_bands.return_value = {}
    mock_fetcher.fh.get_news_sentiment.return_value = {}
    mock_fetcher.fh.get_company_news.return_value = []
    mock_fetcher.fmp.get_earnings_surprises.return_value = []
    mock_fetcher.fh.get_recommendation_trends.return_value = []
    mock_fetcher.fh.get_price_target.return_value = {}

    result = mock_fetcher.fetch("AAPL")

    required_keys = ["ticker", "fetched_at", "quote", "fundamentals",
                     "technicals", "sentiment", "news", "earnings", "analyst"]
    for key in required_keys:
        assert key in result, f"Missing key: {key}"


def test_fetch_handles_api_failure_gracefully(mock_fetcher):
    """If one source fails, others should still return data."""
    mock_fetcher.fh.get_quote.side_effect = Exception("API down")
    mock_fetcher.fmp.get_company_profile.return_value = {"companyName": "Apple"}
    mock_fetcher.fmp.get_key_ratios.return_value = []
    mock_fetcher.fmp.get_income_statement.return_value = []
    mock_fetcher.av.get_rsi.return_value = {}
    mock_fetcher.av.get_macd.return_value = {}
    mock_fetcher.av.get_bollinger_bands.return_value = {}
    mock_fetcher.fh.get_news_sentiment.return_value = {}
    mock_fetcher.fh.get_company_news.return_value = []
    mock_fetcher.fmp.get_earnings_surprises.return_value = []
    mock_fetcher.fh.get_recommendation_trends.return_value = []
    mock_fetcher.fh.get_price_target.return_value = {}

    # Should not raise — just return empty dict for failed source
    result = mock_fetcher.fetch("AAPL")
    assert result["quote"] == {}
    assert result["ticker"] == "AAPL"
