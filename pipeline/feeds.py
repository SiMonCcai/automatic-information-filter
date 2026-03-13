"""
RSS feed parsing and fetching.
Handles feedparser integration with proper error handling.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

import feedparser

logger = logging.getLogger(__name__)


@dataclass
class ParsedArticle:
    """Parsed article from RSS feed."""
    title: str
    url: str
    author: Optional[str]
    content_raw: Optional[str]
    published_at: Optional[str]
    description: Optional[str] = None


class FeedParser:
    """Parse RSS/Atom feeds."""

    def __init__(self, timeout: int = 30, user_agent: str = "RSS-Pipeline/0.1.0"):
        self.timeout = timeout
        self.user_agent = user_agent

    def fetch_and_parse(self, url: str, channel_title: Optional[str] = None) -> tuple[Optional[str], List[ParsedArticle]]:
        """
        Fetch and parse RSS feed.
        Returns (error message, list of articles).
        """
        try:
            parsed = feedparser.parse(
                url,
                agent=self.user_agent,
                request_headers={"User-Agent": self.user_agent}
            )

            # Check for feedparser errors
            if parsed.get("bozo") and parsed.get("bozo_exception"):
                exc = parsed["bozo_exception"]
                logger.warning(f"Feed parse warning for {url}: {exc}")

            # Check for HTTP errors
            status = parsed.get("status")
            if status and status >= 400:
                return f"HTTP {status} error", []

            # Get feed title for author fallback
            feed_title = parsed.feed.get("title", channel_title or "Unknown")

            articles = []
            for entry in parsed.entries:
                try:
                    article = self._parse_entry(entry, feed_title)
                    if article:
                        articles.append(article)
                except Exception as e:
                    logger.warning(f"Error parsing entry in {url}: {e}")
                    continue

            return None, articles

        except Exception as e:
            logger.error(f"Error fetching feed {url}: {e}")
            return str(e), []

    def _parse_entry(self, entry, feed_title: str) -> Optional[ParsedArticle]:
        """Parse a single feed entry."""
        # Title
        title = entry.get("title") or "Untitled"

        # URL/Link - try multiple fields
        link = None
        for link_field in ["link", "id"]:
            if entry.get(link_field):
                link = entry[link_field]
                break
        if not link:
            logger.warning(f"Entry '{title}' has no link, skipping")
            return None

        # Validate URL
        try:
            urlparse(link)
        except Exception:
            logger.warning(f"Entry '{title}' has invalid URL: {link}")
            return None

        # Author - fallback to feed title
        author = entry.get("author")
        if not author:
            # Try author_detail
            author_detail = entry.get("author_detail", {})
            author = author_detail.get("name")
        if not author:
            author = feed_title

        # Published date
        published_at = None
        for date_field in ["published_parsed", "updated_parsed"]:
            if entry.get(date_field):
                try:
                    time_struct = entry[date_field]
                    published_at = datetime(*time_struct[:6]).isoformat()
                    break
                except (TypeError, ValueError):
                    continue

        # Content - prefer content:encoded, then content, then description
        content_raw = None
        if "content" in entry and entry["content"]:
            content_raw = entry["content"][0].get("value")
        elif "description" in entry:
            content_raw = entry["description"]
        elif "summary" in entry:
            content_raw = entry["summary"]

        # Description (for separate storage)
        description = entry.get("summary") or entry.get("description")

        return ParsedArticle(
            title=title,
            url=link,
            author=author,
            content_raw=content_raw,
            published_at=published_at,
            description=description,
        )
