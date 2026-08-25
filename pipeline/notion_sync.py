"""
Notion synchronization - pushes articles to Notion database.
Matches legacy behavior from tmp_aif/app/api/pull/route.ts:
- Content property 內容/内容 must be rich_text chunks split at 1800 chars per chunk
- For published time, write only if property exists and compatible; otherwise skip silently
- Before writing, query database schema once and cache property types
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from zoneinfo import ZoneInfo

try:
    from notion_client import Client as NotionClient
    HAS_NOTION = True
except ImportError:
    HAS_NOTION = False

from .storage import AI_ALL_FIELDS, Article, Storage


logger = logging.getLogger(__name__)


def _is_after_threshold(published_at: str | None, threshold_date: str | None) -> bool:
    """Return True when published_at >= threshold_date (YYYY-MM-DD)."""
    if not threshold_date:
        return True
    if not published_at:
        return False
    try:
        th = datetime.fromisoformat(threshold_date).replace(tzinfo=timezone.utc)
    except Exception:
        return True
    try:
        dt = datetime.fromisoformat(published_at.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return dt >= th

# Content chunk size matching legacy: 1800 chars per chunk
CONTENT_CHUNK_SIZE = 1800
NOTION_RICH_TEXT_MAX_ITEMS = 100
EVENT_FALLBACK_MARKER = "↳ "

FIELD_EVENT_INFO = "事件信息"
FIELD_READ_STATUS = "阅读状态"


class NotionSync:
    """Synchronize articles to Notion database."""

    # Field mapping for Notion
    FIELD_TITLE = "文章名称"
    FIELD_URL = "网址"
    FIELD_AUTHOR = "作者"
    FIELD_CONTENT = "内容"
    FIELD_PUBLISHED_AT = "发布时间"
    FIELD_INGESTED_AT = "导入时间"
    FIELD_PUSH_DATE = "推送日期"
    FIELD_INGEST_VERSION = "ingest_version"
    FIELD_EVENT_INFO = FIELD_EVENT_INFO
    FIELD_READ_STATUS = FIELD_READ_STATUS

    @staticmethod
    def visible_event_title(title: str, member_count: int) -> str:
        """Return the title shown by the representative event page."""
        return title if member_count < 2 else f"【事件·{member_count}】{title}"

    @staticmethod
    def render_event_info(
        members: List[Any], winner_id: Any = None
    ) -> Dict[str, Any]:
        """Render linked event-member lines for a Notion rich-text property."""
        rich_text: list[dict[str, Any]] = [
            {"type": "text", "text": {"content": f"事件成员（{len(members)}）\n"}}
        ]
        member_rich_text: list[list[dict[str, Any]]] = []
        for member in members:
            if isinstance(member, dict):
                get = member.get
            else:
                get = lambda key, default=None, current=member: getattr(current, key, default)
            member_id = get("id") if get("id") is not None else get("article_id")
            winner = member_id == winner_id or bool(get("is_winner", False))
            score = get("score")
            if score in (None, ""):
                score_total = get("score_total")
                score_count = get("score_count")
                if score_total is not None and score_count:
                    score = f"{float(score_total) / int(score_count):.2f}".rstrip("0").rstrip(".")
            line = (
                f"{'⭐ ' if winner else ''}{get('title', '')}"
                f"｜{get('source', '') or '未知来源'}｜{score if score not in (None, '') else '待评分'}\n"
            )
            url = get("url", "")
            member_items: list[dict[str, Any]] = []
            for start in range(0, len(line), 2000):
                item = {
                    "type": "text",
                    "text": {
                        "content": line[start:start + 2000],
                        "link": {"url": url},
                    },
                }
                rich_text.append(item)
                member_items.append(item)
            member_rich_text.append(member_items)

        overflow = len(rich_text) > NOTION_RICH_TEXT_MAX_ITEMS
        return {
            "rich_text": rich_text[:NOTION_RICH_TEXT_MAX_ITEMS],
            "overflow": overflow,
            "fallback_rich_text": rich_text[1:] if overflow else [],
            "fallback_blocks": [NotionSync._event_fallback_block(items) for items in member_rich_text]
            if overflow else [],
        }

    @staticmethod
    def _event_fallback_block(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        copied = [
            {**item, "text": {**item["text"]}}
            for item in items
        ]
        if copied:
            copied[0]["text"]["content"] = EVENT_FALLBACK_MARKER + copied[0]["text"]["content"]
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": copied}}

    def __init__(
        self,
        api_key: str,
        database_id: str,
        page_size: int = 100,
        client: Any = None,
    ):
        if client is None and not HAS_NOTION:
            raise ImportError("notion-client package is required for Notion sync")

        self.client = client if client is not None else NotionClient(auth=api_key)
        self.database_id = database_id
        self.page_size = page_size
        # Cache for database schema
        self._schema_cache: Optional[Dict[str, Any]] = None
        self._data_source_id: Optional[str] = None

    def update_event_page(
        self,
        page_id: str,
        title: str,
        members: List[Any],
        winner_id: Any = None,
        reset_reading: bool = False,
    ) -> Dict[str, Any]:
        """Atomically update event presentation after validating its schema."""
        expected = {
            self.FIELD_TITLE: "title",
            self.FIELD_EVENT_INFO: "rich_text",
        }
        if reset_reading:
            expected[self.FIELD_READ_STATUS] = "checkbox"

        schema = self._fetch_database_schema()
        invalid: list[str] = []
        reasons: list[str] = []
        for name, expected_type in expected.items():
            actual = schema.get(name, {}).get("type") if name in schema else None
            if actual != expected_type:
                invalid.append(name)
                reasons.append(
                    f"{name} expected {expected_type}, got {actual or 'missing'}"
                )
        if invalid:
            error = f"Notion schema mismatch: {'; '.join(reasons)}"
            logger.error(error)
            return {"success": False, "error": error, "invalid_fields": invalid}

        rendered = self.render_event_info(members, winner_id=winner_id)
        visible_title = self.visible_event_title(title, len(members))
        properties: Dict[str, Any] = {
            self.FIELD_TITLE: {"title": [{"text": {"content": visible_title}}]},
            self.FIELD_EVENT_INFO: {"rich_text": rendered["rich_text"]},
        }
        if reset_reading:
            properties[self.FIELD_READ_STATUS] = {"checkbox": False}
        try:
            self.client.pages.update(page_id=page_id, properties=properties)
            self._append_event_fallback_blocks(page_id, rendered)
        except Exception as exc:
            error = f"Failed to update Notion event page {page_id}: {exc}"
            logger.error(error)
            return {"success": False, "error": error, "overflow": rendered["overflow"]}
        return {
            "success": True,
            "overflow": rendered["overflow"],
            "fallback_rich_text": rendered["fallback_rich_text"],
            "fallback_blocks": rendered["fallback_blocks"],
        }

    def apply_representative(
        self,
        page_id: str,
        representative: Any,
        members: List[Any],
        score_meta_values: Dict[str, Any],
        winner_id: Any = None,
    ) -> Dict[str, Any]:
        """Validate a full snapshot, then replace the representative in one update."""
        missing = [name for name in AI_ALL_FIELDS if name not in score_meta_values]
        if missing:
            return {
                "success": False,
                "error": f"Incomplete AI snapshot: {', '.join(missing)}",
                "missing_fields": missing,
            }
        expected = {
            self.FIELD_TITLE: "title",
            self.FIELD_URL: "url",
            self.FIELD_AUTHOR: "rich_text",
            self.FIELD_CONTENT: "rich_text",
            self.FIELD_EVENT_INFO: "rich_text",
            **{name: "rich_text" for name in AI_ALL_FIELDS},
        }
        schema = self._fetch_database_schema()
        invalid = [name for name, kind in expected.items() if schema.get(name, {}).get("type") != kind]
        if invalid:
            return {
                "success": False,
                "error": f"Notion schema mismatch: {', '.join(invalid)}",
                "invalid_fields": invalid,
            }
        properties = self.build_representative_replacement_payload(
            representative, members, score_meta_values, winner_id=winner_id
        )
        try:
            self.client.pages.update(page_id=page_id, properties=properties)
            self._append_event_fallback_blocks(
                page_id,
                self.render_event_info(members, winner_id=winner_id),
            )
        except Exception as exc:
            return {"success": False, "error": f"Failed to apply representative: {exc}"}
        return {"success": True}

    def build_representative_replacement_payload(
        self,
        representative: Any,
        members: List[Any],
        score_meta_values: Dict[str, Any],
        winner_id: Any = None,
    ) -> Dict[str, Any]:
        """Build one properties payload for an atomic representative swap."""
        get = (
            representative.get
            if isinstance(representative, dict)
            else lambda key, default=None: getattr(representative, key, default)
        )
        title = self.visible_event_title(str(get("title", "")), len(members))
        content = get("content_text") or get("content_raw") or ""
        rendered = self.render_event_info(members, winner_id=winner_id)
        properties: Dict[str, Any] = {
            self.FIELD_TITLE: {"title": [{"text": {"content": title}}]},
            self.FIELD_URL: {"url": get("url")},
            self.FIELD_AUTHOR: self._build_rich_text_property(get("author") or "Unknown"),
            self.FIELD_CONTENT: self._build_rich_text_property(content),
            self.FIELD_EVENT_INFO: {"rich_text": rendered["rich_text"]},
        }
        score_meta_fields = list(AI_ALL_FIELDS)
        score_meta_fields.extend(
            name for name in score_meta_values if name not in score_meta_fields
        )
        for field_name in score_meta_fields:
            value = score_meta_values.get(field_name, "")
            properties[field_name] = self._build_rich_text_property(str(value or ""))
        return properties

    def _append_event_fallback_blocks(self, page_id: str, rendered: Dict[str, Any]) -> None:
        """Replace our marked overflow blocks so retries cannot duplicate links."""
        blocks = list(rendered.get("fallback_blocks") or [])
        if blocks:
            self._delete_existing_event_fallback_blocks(page_id)
        for start in range(0, len(blocks), NOTION_RICH_TEXT_MAX_ITEMS):
            self.client.blocks.children.append(
                block_id=page_id,
                children=blocks[start:start + NOTION_RICH_TEXT_MAX_ITEMS],
            )

    def _delete_existing_event_fallback_blocks(self, page_id: str) -> None:
        cursor = None
        marked_ids: list[str] = []
        while True:
            kwargs: Dict[str, Any] = {"block_id": page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = self.client.blocks.children.list(**kwargs)
            for block in response.get("results") or []:
                rich_text = ((block.get("paragraph") or {}).get("rich_text") or [])
                content = ((rich_text[0].get("text") or {}).get("content") if rich_text else "") or ""
                if content.startswith(EVENT_FALLBACK_MARKER):
                    marked_ids.append(block["id"])
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
        for block_id in marked_ids:
            self.client.blocks.delete(block_id=block_id)

    def _fetch_database_schema(self) -> Dict[str, Any]:
        """Fetch and cache schema (supports legacy databases + new data_sources)."""
        if self._schema_cache is None:
            try:
                db = self.client.databases.retrieve(self.database_id)
                props = db.get("properties") or {}

                # New Notion response may expose schema under data_sources[*].
                if not props:
                    ds_list = db.get("data_sources") or []
                    if ds_list and isinstance(ds_list, list):
                        ds_id = ds_list[0].get("id")
                        if ds_id and hasattr(self.client, "data_sources"):
                            self._data_source_id = ds_id
                            ds = self.client.data_sources.retrieve(data_source_id=ds_id)
                            props = ds.get("properties") or {}

                self._schema_cache = props
                logger.debug(f"Fetched schema with {len(self._schema_cache)} properties")
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

    def _build_rich_text_property(self, content: str) -> dict[str, list[dict[str, Any]]]:
        chunks = self._split_content_chunks(content or "")
        return {
            "rich_text": [
                {"type": "text", "text": {"content": chunk}}
                for chunk in chunks
            ]
        }

    def update_rich_text_properties(self, page_id: str, values: Dict[str, str]) -> list[str]:
        properties: Dict[str, Any] = {}
        skipped: list[str] = []
        for field_name, value in values.items():
            if not self._has_property(field_name, "rich_text"):
                skipped.append(field_name)
                continue
            properties[field_name] = self._build_rich_text_property(value)
        if not properties:
            return skipped
        self.client.pages.update(page_id=page_id, properties=properties)
        return skipped

    def find_page_by_url(self, url: str) -> Optional[str]:
        """Recover a page created before its ID was durably persisted locally."""
        self._fetch_database_schema()
        if self._data_source_id and hasattr(self.client, "data_sources"):
            response = self.client.data_sources.query(
                data_source_id=self._data_source_id,
                filter={"property": self.FIELD_URL, "url": {"equals": url}},
                page_size=1,
            )
        elif hasattr(self.client.databases, "query"):
            response = self.client.databases.query(
                database_id=self.database_id,
                filter={"property": self.FIELD_URL, "url": {"equals": url}},
                page_size=1,
            )
        else:
            raise RuntimeError("Notion client cannot query pages for URL recovery")
        results = response.get("results") or []
        return results[0].get("id") if results else None

    def sync_articles(
        self,
        articles: List[Article],
        recover_existing_article_ids: Optional[set[int]] = None,
    ) -> tuple[int, List[str], dict[int, str]]:
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
                page_id = None
                if recover_existing_article_ids and article.id in recover_existing_article_ids:
                    page_id = self.find_page_by_url(article.url)
                if not page_id:
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

        # Record push date if property exists and is a date field.
        if self._has_property(self.FIELD_PUSH_DATE, "date"):
            shanghai_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            properties[self.FIELD_PUSH_DATE] = {
                "date": {"start": shanghai_date}
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

        page_id = response["id"]
        return page_id

    def _touch_ingested_at(self, page_id: str) -> None:
        """Update ingestion timestamp to emit a reliable page.updated event."""
        if not self._has_property(self.FIELD_INGESTED_AT, "date"):
            logger.debug(f"Skip touch: property '{self.FIELD_INGESTED_AT}' not found or not date type")
            return

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            self.client.pages.update(
                page_id=page_id,
                properties={
                    self.FIELD_INGESTED_AT: {
                        "date": {"start": now_iso}
                    }
                },
            )
            logger.debug(f"Touched '{self.FIELD_INGESTED_AT}' for page {page_id}")
        except Exception as e:
            logger.warning(f"Failed to touch '{self.FIELD_INGESTED_AT}' on page {page_id}: {e}")
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
    sync_published_after: str | None = None,
    sync_scan_limit: int = 100,
    event_mode_enabled: bool = True,
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
    scan_limit = max(batch_size, sync_scan_limit)
    articles = storage.get_unsynced_articles(limit=scan_limit, event_mode=event_mode_enabled)

    # Optional published_at threshold filter (e.g. 2026-03-01)
    eligible_articles = [a for a in articles if _is_after_threshold(a.published_at, sync_published_after)]

    eligible_articles = eligible_articles[:batch_size]

    if not eligible_articles:
        logger.info("No eligible unsynced articles found for current threshold")
        return {
            "success": True,
            "synced": 0,
            "pending": len(articles),
            "skipped_by_date": len(articles),
            "threshold": sync_published_after,
        }

    if not articles:
        logger.info("No unsynced articles found")
        return {
            "success": True,
            "synced": 0,
            "pending": 0,
        }

    logger.info(f"Syncing {len(eligible_articles)} articles to Notion...")

    if dry_run:
        return {
            "success": True,
            "synced": 0,
            "pending": len(eligible_articles),
            "dry_run": True,
        }

    recovery_ids = {
        article.id
        for article in eligible_articles
        if storage.get_event_id_for_article(article.id) is not None
    }
    synced, errors, page_map = syncer.sync_articles(
        eligible_articles,
        recover_existing_article_ids=recovery_ids,
    )

    # Mark synced articles with real Notion page IDs
    for article in eligible_articles:
        page_id = page_map.get(article.id)
        if page_id:
            storage.mark_article_synced(article.id, page_id)

    synced_article_ids = [article.id for article in eligible_articles if article.id in page_map]

    return {
        "success": True,
        "synced": synced,
        "pending": len(storage.get_unsynced_articles(limit=batch_size, event_mode=event_mode_enabled)),
        "errors": errors,
        "synced_article_ids": synced_article_ids,
        "synced_page_map": page_map,
    }
