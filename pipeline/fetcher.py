"""
Feed fetcher - orchestrates fetching from multiple feeds.
"""

import hashlib
import logging
import re
import time
from datetime import datetime
from typing import List, Optional
from .cleaner import HTMLCleaner
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Config
from .feeds import FeedParser, ParsedArticle
from .storage import Storage, Feed


logger = logging.getLogger(__name__)

TRACKING_QUERY_PREFIXES = (
    "utm_", "spm", "from", "source", "ref", "fbclid", "gclid", "igshid", "mkt_tok"
)
HACKER_NEWS_HOSTS = ("hnrss.org", "news.ycombinator.com")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
METADATA_ONLY_LINE_RE = re.compile(
    r"^(?:article\s+url|comments?\s+url|points?|#\s*comments?|source|link|read\s+more)\s*:\s*.*$",
    re.IGNORECASE,
)
PURE_METADATA_VALUE_RE = re.compile(r"^(?:#\s*comments?\s*:\s*\d+|points?\s*:\s*\d+)$", re.IGNORECASE)
MIN_SUBSTANTIVE_TEXT_CHARS = 80


def normalize_url(url: str) -> str:
    """Normalize URL for more stable dedup across tracking params and fragments."""
    try:
        p = urlsplit(url.strip())
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path or "/"

        kept = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            lk = k.lower()
            if lk.startswith(TRACKING_QUERY_PREFIXES):
                continue
            kept.append((k, v))
        kept.sort(key=lambda x: (x[0], x[1]))
        query = urlencode(kept, doseq=True)

        # Remove fragment entirely
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return url


def make_fingerprint(parsed: ParsedArticle, author: str) -> str:
    """Create stable dedup fingerprint for multi-source feeds."""
    norm_url = normalize_url(parsed.url)
    identity = parsed.guid or norm_url
    content_hint = (parsed.content_raw or parsed.description or "")[:500]
    data = "\n".join([
        identity or "",
        parsed.title or "",
        parsed.published_at or "",
        author or "",
        content_hint,
    ])
    return hashlib.sha256(data.encode("utf-8", errors="ignore")).hexdigest()


def normalize_match_text(text: Optional[str]) -> str:
    """Normalize text for keyword contains checks."""
    return " ".join((text or "").casefold().split())


def first_matching_keyword(text: str, keywords: list[str]) -> Optional[str]:
    """Return first keyword whose normalized form is contained in text."""
    for keyword in keywords:
        normalized_keyword = normalize_match_text(keyword)
        if normalized_keyword and normalized_keyword in text:
            return keyword
    return None


def is_hacker_news_feed(feed: Optional[Feed]) -> bool:
    """Return True when the configured feed is a Hacker News source."""
    if not feed:
        return False
    name = (feed.name or "").casefold()
    host = (urlsplit(feed.url).netloc or "").casefold()
    return name.startswith("hacker news") or any(host.endswith(domain) for domain in HACKER_NEWS_HOSTS)


def detect_link_only_content(body: str) -> Optional[str]:
    """Return discard reason when content is effectively metadata/links only."""
    body = (body or "").strip()
    if not body:
        return "empty_content"

    raw_lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not raw_lines:
        return "empty_content"

    metadata_lines = 0
    substantive_lines: list[str] = []
    url_count = len(URL_RE.findall(body))

    for line in raw_lines:
        lowered = line.lower()
        if METADATA_ONLY_LINE_RE.match(lowered) or PURE_METADATA_VALUE_RE.match(lowered):
            metadata_lines += 1
            continue

        cleaned_line = URL_RE.sub(" ", line)
        cleaned_line = re.sub(r"\s+", " ", cleaned_line).strip(" :-•\t")
        if cleaned_line:
            substantive_lines.append(cleaned_line)

    cleaned_text = "\n".join(substantive_lines)
    cleaned_chars = len(re.sub(r"\s+", "", cleaned_text))

    if metadata_lines >= max(2, len(raw_lines) - 1) and cleaned_chars < MIN_SUBSTANTIVE_TEXT_CHARS:
        return "metadata_only_content"

    if url_count >= 1 and cleaned_chars < MIN_SUBSTANTIVE_TEXT_CHARS:
        return "insufficient_text_after_url_strip"

    return None


class Fetcher:
    """Fetch articles from configured feeds."""

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config
        self.parser = FeedParser(
            timeout=config.fetch_timeout,
            user_agent=config.fetch_user_agent
        )
        self.cleaner = HTMLCleaner()

    def fetch_feed(self, feed: Feed) -> tuple[int, int, int, Optional[str]]:
        """
        Fetch articles from a single feed.
        Returns (articles_added, articles_skipped, articles_filtered, error_message).
        """
        logger.info(f"Fetching feed: {feed.name} ({feed.url})")

        error, articles = self.parser.fetch_and_parse(feed.url, feed.name)

        if error:
            self.storage.update_feed_fetch(feed.id, error)
            return 0, 0, 0, error

        added = 0
        skipped = 0
        filtered = 0

        for parsed in articles:
            try:
                outcome = self._add_article(feed.id, parsed)
                if outcome == "added":
                    added += 1
                elif outcome == "filtered":
                    filtered += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning(f"Error adding article from {feed.name}: {e}")
                skipped += 1

        self.storage.update_feed_fetch(feed.id, None)
        logger.info(
            f"Feed {feed.name}: {added} added, {skipped} skipped (duplicate/error), {filtered} filtered"
        )

        return added, skipped, filtered, None

    def _clean_content_source(self, parsed: ParsedArticle) -> str:
        """Return cleaned plain text from parsed article content."""
        content_source = parsed.content_raw or parsed.description or ""
        return self.cleaner.clean(content_source) or content_source or ""

    def _match_filter_rule(self, feed: Optional[Feed], parsed: ParsedArticle) -> Optional[tuple[str, str]]:
        """Return (matched_field, keyword) when a discard rule matches."""
        if not feed:
            return None
        filters = self.storage.get_feed_keyword_filters(feed.id)
        title_keywords = filters["title_keywords"]
        content_keywords = filters["content_keywords"]

        if title_keywords:
            title_text = normalize_match_text(parsed.title)
            matched_keyword = first_matching_keyword(title_text, title_keywords)
            if matched_keyword:
                return "title", matched_keyword

        if content_keywords:
            content_text = normalize_match_text(self._clean_content_source(parsed))
            matched_keyword = first_matching_keyword(content_text, content_keywords)
            if matched_keyword:
                return "content", matched_keyword

        return None

    def _match_hacker_news_link_only_discard(self, feed: Optional[Feed], parsed: ParsedArticle) -> Optional[str]:
        """Discard Hacker News items whose body is only metadata/links."""
        if not is_hacker_news_feed(feed):
            return None
        return detect_link_only_content(self._clean_content_source(parsed))

    def _add_article(self, feed_id: int, parsed: ParsedArticle) -> str:
        """Add parsed article to storage and return outcome: added/skipped/filtered."""
        # URL duplicate (global unique)
        normalized = normalize_url(parsed.url)
        existing = self.storage.get_article_by_url(parsed.url)
        if existing:
            logger.debug(f"Article already exists by raw URL: {parsed.url}")
            return "skipped"
        existing_tombstone = self.storage.get_dedup_tombstone_by_url(parsed.url)
        if existing_tombstone:
            logger.debug(f"Article already exists by raw URL tombstone: {parsed.url}")
            return "skipped"
        existing_norm = self.storage.get_article_by_url(normalized)
        if existing_norm:
            logger.debug(f"Article already exists by normalized URL: {normalized}")
            return "skipped"
        existing_norm_tombstone = self.storage.get_dedup_tombstone_by_url(normalized)
        if existing_norm_tombstone:
            logger.debug(f"Article already exists by normalized URL tombstone: {normalized}")
            return "skipped"

        # Get feed for default_author
        feed = self.storage.get_feed(feed_id)
        default_author = feed.default_author if feed else None

        # Use default_author if parsed author is empty
        author = parsed.author or default_author

        # Create fingerprint for content deduplication
        fingerprint = None
        if self.config.enable_content_fingerprint:
            fingerprint = make_fingerprint(parsed, author or "unknown")

            # Check for fingerprint duplicate across feeds
            existing_by_fp = self.storage.get_article_by_fingerprint(fingerprint)
            if existing_by_fp:
                logger.debug(f"Duplicate article by fingerprint: {parsed.url}")
                return "skipped"
            existing_tombstone_by_fp = self.storage.get_dedup_tombstone_by_fingerprint(fingerprint)
            if existing_tombstone_by_fp:
                logger.debug(f"Duplicate article by fingerprint tombstone: {parsed.url}")
                return "skipped"

        matched = self._match_filter_rule(feed, parsed)
        if matched:
            matched_field, matched_keyword = matched
            self.storage.add_article_discard_log(
                feed_id=feed_id,
                feed_name=feed.name if feed else None,
                article_title=parsed.title,
                article_url=normalized,
                matched_field=matched_field,
                matched_keyword=matched_keyword,
                discard_reason="keyword_filter",
            )
            logger.info(
                "Discarded article from feed %s by %s keyword match: %s",
                feed.name if feed else feed_id,
                matched_field,
                matched_keyword,
            )
            return "filtered"

        hn_discard_reason = self._match_hacker_news_link_only_discard(feed, parsed)
        if hn_discard_reason:
            self.storage.add_article_discard_log(
                feed_id=feed_id,
                feed_name=feed.name if feed else None,
                article_title=parsed.title,
                article_url=normalized,
                matched_field="content",
                matched_keyword=hn_discard_reason,
                discard_reason="hacker_news_link_only",
            )
            logger.info(
                "Discarded Hacker News article from feed %s due to link-only body: %s",
                feed.name if feed else feed_id,
                hn_discard_reason,
            )
            return "filtered"

        created = self.storage.add_article(
            feed_id=feed_id,
            title=parsed.title,
            url=normalized,
            author=author,
            content_raw=parsed.content_raw,
            content_text=None,  # Will be cleaned later
            published_at=parsed.published_at,
            fingerprint=fingerprint,
        )
        return "added" if created else "skipped"

    def fetch_all(self, enabled_only: bool = True) -> dict[str, any]:
        """
        Fetch from all configured feeds.
        Returns summary dict with stats.
        """
        feeds = self.storage.list_feeds(enabled_only=enabled_only)

        total_added = 0
        total_skipped = 0
        total_filtered = 0
        errors = []

        for feed in feeds:
            try:
                added, skipped, filtered, error = self.fetch_feed(feed)
                total_added += added
                total_skipped += skipped
                total_filtered += filtered
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
            "articles_filtered": total_filtered,
            "errors": errors,
            "timestamp": datetime.now().isoformat(),
        }
