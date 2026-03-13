"""
Feed fetcher - orchestrates fetching from multiple feeds.
"""

import hashlib
import logging
import time
from datetime import datetime
from typing import List, Optional

from .config import Config
from .feeds import FeedParser, ParsedArticle
from .storage import Storage, Feed, Article


logger = logging.getLogger(__name__)


def make_fingerprint(title: str, published_at: Optional[str], author: str) -> str:
    """Create content fingerprint for deduplication."""
    data = f"{title}{published_at or ''}{author}"
    return hashlib.sha256(data.encode()).hexdigest()


class Fetcher:
    """Fetch articles from configured feeds."""

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config
        self.parser = FeedParser(
            timeout=config.fetch_timeout,
            user_agent=config.fetch_user_agent
        )

    def fetch_feed(self, feed: Feed) -> tuple[int, int, Optional[str]]:
        """
        Fetch articles from a single feed.
        Returns (articles_added, articles_skipped, error_message).
        """
        logger.info(f"Fetching feed: {feed.name} ({feed.url})")

        error, articles = self.parser.fetch_and_parse(feed.url, feed.name)

        if error:
            self.storage.update_feed_fetch(feed.id, error)
            return 0, 0, error

        added = 0
        skipped = 0

        for parsed in articles:
            try:
                article = self._add_article(feed.id, parsed)
                if article:
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning(f"Error adding article from {feed.name}: {e}")
                skipped += 1

        self.storage.update_feed_fetch(feed.id, None)
        logger.info(f"Feed {feed.name}: {added} added, {skipped} skipped (duplicate)")

        return added, skipped, None

    def _add_article(self, feed_id: int, parsed: ParsedArticle) -> Optional[Article]:
        """Add parsed article to storage."""
        # Check for URL duplicate (globally unique across all feeds)
        existing = self.storage.get_article_by_url(parsed.url)
        if existing:
            logger.debug(f"Article already exists: {parsed.url}")
            return None

        # Get feed for default_author
        feed = self.storage.get_feed(feed_id)
        default_author = feed.default_author if feed else None

        # Use default_author if parsed author is empty
        author = parsed.author or default_author

        # Create fingerprint for content deduplication
        fingerprint = None
        if self.config.enable_content_fingerprint:
            fingerprint = make_fingerprint(
                parsed.title,
                parsed.published_at,
                author or "unknown"
            )

            # Check for fingerprint duplicate across feeds
            existing_by_fp = self.storage.get_article_by_fingerprint(fingerprint)
            if existing_by_fp:
                logger.debug(f"Duplicate article by fingerprint: {parsed.url}")
                return None

        return self.storage.add_article(
            feed_id=feed_id,
            title=parsed.title,
            url=parsed.url,
            author=author,
            content_raw=parsed.content_raw,
            content_text=None,  # Will be cleaned later
            published_at=parsed.published_at,
            fingerprint=fingerprint,
        )

    def fetch_all(self, enabled_only: bool = True) -> dict[str, any]:
        """
        Fetch from all configured feeds.
        Returns summary dict with stats.
        """
        feeds = self.storage.list_feeds(enabled_only=enabled_only)

        total_added = 0
        total_skipped = 0
        errors = []

        for feed in feeds:
            try:
                added, skipped, error = self.fetch_feed(feed)
                total_added += added
                total_skipped += skipped
                if error:
                    errors.append(f"{feed.name}: {error}")

                # Rate limiting
                if self.config.request_delay > 0:
                    time.sleep(self.config.request_delay)

            except Exception as e:
                logger.error(f"Error processing feed {feed.name}: {e}")
                errors.append(f"{feed.name}: {str(e)}")

        return {
            "feeds_processed": len(feeds),
            "articles_added": total_added,
            "articles_skipped": total_skipped,
            "errors": errors,
            "timestamp": datetime.now().isoformat(),
        }
