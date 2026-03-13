"""
Notion synchronization - pushes articles to Notion database.
Matches legacy behavior from tmp_aif/app/api/pull/route.ts:
- Content property 內容/内容 must be rich_text chunks split at 1800 chars per chunk
- For published time, write only if property exists and compatible; otherwise skip silently
- Before writing, query database schema once and cache property types
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

try:
    from notion_client import Client as NotionClient
    HAS_NOTION = True
except ImportError:
    HAS_NOTION = False

from .storage import Article, Storage


logger = logging.getLogger(__name__)

# Content chunk size matching legacy: 1800 chars per chunk
CONTENT_CHUNK_SIZE = 1800


class NotionSync:
    """Synchronize articles to Notion database."""

    # Field mapping for Notion
    FIELD_TITLE = "文章名称"
    FIELD_URL = "网址"
    FIELD_AUTHOR = "作者"
    FIELD_CONTENT = "内容"
    FIELD_PUBLISHED_AT = "发布时间"

    def __init__(self, api_key: str, database_id: str, page_size: int = 100):
        if not HAS_NOTION:
            raise ImportError("notion-client package is required for Notion sync")

        self.client = NotionClient(auth=api_key)
        self.database_id = database_id
        self.page_size = page_size
        # Cache for database schema
        self._schema_cache: Optional[Dict[str, Any]] = None

    def _fetch_database_schema(self) -> Dict[str, Any]:
        """Fetch and cache database schema."""
        if self._schema_cache is None:
            try:
                db = self.client.databases.retrieve(self.database_id)
                self._schema_cache = db.get("properties", {})
                logger.debug(f"Fetched database schema with {len(self._schema_cache)} properties")
            except Exception as e:
                logger.warning(f"Failed to fetch database schema: {e}")
                self._schema_cache = {}
        return self._schema_cache

    def _has_property(self, property_name: str, expected_type: Optional[str] = None) -> bool:
        """Check if a property exists and optionally matches expected type."""
        schema = self._fetch_database_schema()
        if property_name not in schema:
            return False
        if expected_type:
            prop_type = schema[property_name].get("type")
            return prop_type == expected_type
        return True

    def _split_content_chunks(self, content: str, chunk_size: int = CONTENT_CHUNK_SIZE) -> List[str]:
        """
        Split content into chunks for Notion rich_text property.
        Matches legacy splitTextForRichText behavior.
        """
        if not content:
            return []
        chunks = []
        current_pos = 0
        while current_pos < len(content):
            chunks.append(content[current_pos:current_pos + chunk_size])
            current_pos += chunk_size
        return chunks

    def sync_articles(self, articles: List[Article]) -> tuple[int, List[str], dict[int, str]]:
        """
        Sync articles to Notion.
        Returns (count_synced, list_of_errors, article_id_to_notion_page_id).
        """
        # Fetch schema once at start
        self._fetch_database_schema()

        synced = 0
        errors = []
        page_map: dict[int, str] = {}

        for article in articles:
            try:
                page_id = self._create_page(article)
                synced += 1
                page_map[article.id] = page_id
                logger.info(f"Synced article: {article.title} -> {page_id}")
            except Exception as e:
                error_msg = f"Error syncing '{article.title}': {e}"
                logger.error(error_msg)
                errors.append(error_msg)

        return synced, errors, page_map

    def _create_page(self, article: Article) -> str:
        """Create a Notion page for an article."""
        # Prepare content - split into 1800 char chunks
        content = article.content_text or article.content_raw or ""
        content_chunks = []

        if content:
            chunks = self._split_content_chunks(content)
            content_chunks = [{"type": "text", "text": {"content": chunk}} for chunk in chunks]

        # Build properties
        properties = {
            self.FIELD_TITLE: {
                "title": [{"text": {"content": article.title}}]
            },
            self.FIELD_URL: {
                "url": article.url
            },
            self.FIELD_AUTHOR: {
                "rich_text": [{"text": {"content": article.author or "Unknown"}}]
            },
        }

        # Only add published_at if property exists and is compatible type
        if article.published_at and self._has_property(self.FIELD_PUBLISHED_AT, "date"):
            try:
                # Parse ISO datetime
                dt = datetime.fromisoformat(article.published_at.replace("Z", "+00:00"))
                properties[self.FIELD_PUBLISHED_AT] = {
                    "date": {"start": dt.isoformat()}
                }
            except Exception as e:
                logger.debug(f"Error parsing published_at for '{article.title}': {e}")

        # Add content if chunks available (always include; legacy-compatible behavior)
        # NOTE: Some Notion API versions return database metadata without `properties`
        # on /databases/{id}, which can cause schema checks to false-negative.
        # To match legacy behavior, we always attempt to write `内容`.
        if content_chunks:
            properties[self.FIELD_CONTENT] = {
                "rich_text": content_chunks
            }

        response = self.client.pages.create(
            parent={"database_id": self.database_id},
            properties=properties
        )

        return response["id"]

    def test_connection(self) -> bool:
        """Test Notion API connection."""
        try:
            self.client.databases.retrieve(self.database_id)
            return True
        except Exception as e:
            logger.error(f"Notion connection test failed: {e}")
            return False


def sync_articles_to_notion(
    storage: Storage,
    api_key: str,
    database_id: str,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, any]:
    """
    Main sync function - fetches unsynced articles and pushes to Notion.
    """
    if not HAS_NOTION:
        return {
            "success": False,
            "error": "notion-client package not installed",
            "synced": 0,
        }

    syncer = NotionSync(api_key, database_id)

    # Test connection
    if not syncer.test_connection():
        return {
            "success": False,
            "error": "Failed to connect to Notion",
            "synced": 0,
        }

    # Get unsynced articles
    articles = storage.get_unsynced_articles(limit=batch_size)

    if not articles:
        logger.info("No unsynced articles found")
        return {
            "success": True,
            "synced": 0,
            "pending": 0,
        }

    logger.info(f"Syncing {len(articles)} articles to Notion...")

    if dry_run:
        return {
            "success": True,
            "synced": 0,
            "pending": len(articles),
            "dry_run": True,
        }

    synced, errors, page_map = syncer.sync_articles(articles)

    # Mark synced articles with real Notion page IDs
    for article in articles:
        page_id = page_map.get(article.id)
        if page_id:
            storage.mark_article_synced(article.id, page_id)

    return {
        "success": True,
        "synced": synced,
        "pending": storage.get_unsynced_articles(limit=batch_size).__len__(),
        "errors": errors,
    }
