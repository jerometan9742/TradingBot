"""
CNN Fear & Greed Index connector — completely free, no API key required.

Score range: 0 (Extreme Fear) → 100 (Extreme Greed).
Endpoint: https://production.dataviz.cnn.io/index/fearandgreed/graphdata
Requires browser-style headers; returns 418 for bare bot requests.
"""

import logging

import requests

logger = logging.getLogger(__name__)

_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}


class FearGreedClient:
    def get(self) -> dict:
        """
        Fetch current Fear & Greed score from CNN.

        Returns:
            {
                "score":           float,   # 0-100
                "rating":          str,     # e.g. "greed", "extreme fear"
                "previous_close":  float,
                "previous_1_week": float,
                "previous_1_month": float,
            }
        """
        response = requests.get(_URL, headers=_HEADERS, timeout=10)
        response.raise_for_status()
        fg = response.json().get("fear_and_greed", {})
        return {
            "score": fg.get("score"),
            "rating": fg.get("rating"),
            "previous_close": fg.get("previous_close"),
            "previous_1_week": fg.get("previous_1_week"),
            "previous_1_month": fg.get("previous_1_month"),
        }
