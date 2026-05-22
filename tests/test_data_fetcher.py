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


# ---------------------------------------------------------------------------
# AlphaVantageClient.get_adx() unit tests
# ---------------------------------------------------------------------------

def test_get_adx_returns_float():
    """get_adx() returns a positive float when the API responds correctly."""
    import os
    from unittest.mock import patch, MagicMock
    from data.alpha_vantage import AlphaVantageClient

    sample = {
        "Technical Analysis: ADX": {
            "2025-05-21": {"ADX": "28.1234"},
            "2025-05-20": {"ADX": "27.5000"},
        }
    }
    os.environ.setdefault("ALPHA_VANTAGE_API_KEY", "test_key")
    with patch("data.alpha_vantage.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = sample
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        client = AlphaVantageClient()
        result = client.get_adx("AAPL")

    assert isinstance(result, float), f"Expected float, got {type(result)}"
    assert result > 0, f"Expected positive ADX, got {result}"


def test_get_adx_returns_none_on_api_error():
    """get_adx() returns None gracefully when the API call fails."""
    import os
    from unittest.mock import patch
    from data.alpha_vantage import AlphaVantageClient

    os.environ.setdefault("ALPHA_VANTAGE_API_KEY", "test_key")
    with patch("data.alpha_vantage.requests.get", side_effect=Exception("Network error")):
        client = AlphaVantageClient()
        result = client.get_adx("AAPL")

    assert result is None


def test_get_adx_returns_none_on_empty_series():
    """get_adx() returns None when Alpha Vantage returns an empty data series."""
    import os
    from unittest.mock import patch, MagicMock
    from data.alpha_vantage import AlphaVantageClient

    os.environ.setdefault("ALPHA_VANTAGE_API_KEY", "test_key")
    with patch("data.alpha_vantage.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"Technical Analysis: ADX": {}}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        client = AlphaVantageClient()
        result = client.get_adx("AAPL")

    assert result is None
