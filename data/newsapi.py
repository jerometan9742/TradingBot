"""
NewsAPI connector — free tier: 100 req/day, up to 1 month history.
Sign up at newsapi.org and set NEWSAPI_KEY in .env.
"""

import logging
import os

from newsapi import NewsApiClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class NewsAPIConnector:
    def __init__(self):
        api_key = os.getenv("NEWSAPI_KEY")
        if not api_key:
            raise ValueError("NEWSAPI_KEY not set in .env")
        self._client = NewsApiClient(api_key=api_key)

    def get_articles(self, ticker: str, company_name: str = "", page_size: int = 10) -> list:
        """
        Fetch recent English-language news articles.

        Uses company_name if provided (more reliable than raw ticker symbol).
        Returns normalised dicts matching the fetcher news schema.
        """
        query = company_name or ticker
        try:
            resp = self._client.get_everything(
                q=query,
                language="en",
                sort_by="publishedAt",
                page_size=page_size,
            )
            articles = resp.get("articles", [])
            return [
                {
                    "headline": a.get("title"),
                    "source": (a.get("source") or {}).get("name"),
                    "datetime": a.get("publishedAt"),
                    "url": a.get("url"),
                    "summary": (a.get("description") or "")[:300],
                }
                for a in articles
                if a.get("title") and "[Removed]" not in a.get("title", "")
            ]
        except Exception as e:
            logger.warning("[NewsAPI] get_articles failed for %s: %s", ticker, e)
            return []
