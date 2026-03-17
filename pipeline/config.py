"""
Configuration management for the RSS pipeline.
Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """Pipeline configuration."""

    # Database
    db_path: str = "pipeline.db"

    # Notion
    notion_api_key: Optional[str] = None
    notion_database_id: Optional[str] = None
    notion_page_size: int = 100

    # Fetching
    fetch_timeout: int = 30
    fetch_user_agent: str = "RSS-Pipeline/0.1.0"
    request_delay: float = 1.0

    # Deduplication
    enable_content_fingerprint: bool = True

    # Logging
    log_level: str = "INFO"

    # Sync filter
    sync_published_after: Optional[str] = None  # ISO date, e.g. 2026-03-01
    sync_scan_limit: int = 100  # how many unsynced rows to scan before filtering

    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        return cls(
            db_path=os.getenv("PIPELINE_DB_PATH", "pipeline.db"),
            notion_api_key=os.getenv("NOTION_API_KEY"),
            notion_database_id=os.getenv("NOTION_DATABASE_ID"),
            notion_page_size=int(os.getenv("NOTION_PAGE_SIZE", "100")),
            fetch_timeout=int(os.getenv("FETCH_TIMEOUT", "30")),
            fetch_user_agent=os.getenv("FETCH_USER_AGENT", "RSS-Pipeline/0.1.0"),
            request_delay=float(os.getenv("REQUEST_DELAY", "1.0")),
            enable_content_fingerprint=os.getenv("ENABLE_CONTENT_FINGERPRINT", "true").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            sync_published_after=os.getenv("SYNC_PUBLISHED_AFTER"),
            sync_scan_limit=int(os.getenv("SYNC_SCAN_LIMIT", "100")),
        )

    def validate_for_sync(self) -> None:
        """Validate configuration for Notion sync."""
        if not self.notion_api_key:
            raise ValueError("NOTION_API_KEY is required for Notion sync")
        if not self.notion_database_id:
            raise ValueError("NOTION_DATABASE_ID is required for Notion sync")
