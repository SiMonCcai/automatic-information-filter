"""
Configuration management for the RSS pipeline.
Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv


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

    # Event-level title deduplication (disabled until production rollout)
    event_dedup_enabled: bool = False
    event_dedup_threshold: float = 0.96
    event_dedup_window_days: int = 7
    event_winner_margin_total: int = 2
    event_embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    event_embedding_python: str = "/root/rss-pipeline/.venv-embedding/bin/python"
    event_embedding_cache: str = "/root/rss-pipeline/.embedding-cache"
    event_embedding_batch_size: int = 8
    event_embedding_threads: int = 1

    # Logging
    log_level: str = "INFO"

    # AI enrichment provider selection
    ai_provider: str = "deepseek"
    ai_enrichment_batch_size: int = 10
    ai_worker_batch_size: int = 20

    # DeepSeek enrichment
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout: int = 90
    deepseek_max_retries: int = 3
    deepseek_max_concurrency: int = 7

    # GLM enrichment
    glm_api_key: Optional[str] = None
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4.7"
    glm_timeout: int = 60
    glm_max_retries: int = 2
    glm_max_concurrency: int = 1
    glm_min_request_interval: float = 2.0
    glm_rate_limit_cooldown: float = 8.0

    # Sync filter
    sync_published_after: Optional[str] = None  # ISO date, e.g. 2026-03-01
    sync_scan_limit: int = 100  # how many unsynced rows to scan before filtering

    @classmethod
    def from_env(cls) -> "Config":
        """Create configuration from environment variables."""
        # Ensure .env is loaded when running via CLI/service without exported env vars
        env_path = Path(__file__).resolve().parents[1] / ".env"
        load_dotenv(env_path, override=False)

        return cls(
            db_path=os.getenv("PIPELINE_DB_PATH", "pipeline.db"),
            notion_api_key=os.getenv("NOTION_API_KEY"),
            notion_database_id=os.getenv("NOTION_DATABASE_ID"),
            notion_page_size=int(os.getenv("NOTION_PAGE_SIZE", "100")),
            fetch_timeout=int(os.getenv("FETCH_TIMEOUT", "30")),
            fetch_user_agent=os.getenv("FETCH_USER_AGENT", "RSS-Pipeline/0.1.0"),
            request_delay=float(os.getenv("REQUEST_DELAY", "1.0")),
            enable_content_fingerprint=os.getenv("ENABLE_CONTENT_FINGERPRINT", "true").lower() == "true",
            event_dedup_enabled=os.getenv("EVENT_DEDUP_ENABLED", "false").lower() == "true",
            event_dedup_threshold=float(os.getenv("EVENT_DEDUP_THRESHOLD", "0.96")),
            event_dedup_window_days=int(os.getenv("EVENT_DEDUP_WINDOW_DAYS", "7")),
            event_winner_margin_total=int(os.getenv("EVENT_WINNER_MARGIN_TOTAL", "2")),
            event_embedding_model=os.getenv(
                "EVENT_EMBEDDING_MODEL",
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            ),
            event_embedding_python=os.getenv(
                "EVENT_EMBEDDING_PYTHON",
                "/root/rss-pipeline/.venv-embedding/bin/python",
            ),
            event_embedding_cache=os.getenv(
                "EVENT_EMBEDDING_CACHE",
                "/root/rss-pipeline/.embedding-cache",
            ),
            event_embedding_batch_size=int(os.getenv("EVENT_EMBEDDING_BATCH_SIZE", "8")),
            event_embedding_threads=int(os.getenv("EVENT_EMBEDDING_THREADS", "1")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            ai_provider=os.getenv("AI_PROVIDER", "deepseek").strip().lower(),
            ai_enrichment_batch_size=int(os.getenv("AI_ENRICHMENT_BATCH_SIZE", "4")),
            ai_worker_batch_size=int(os.getenv("AI_WORKER_BATCH_SIZE", "20")),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_timeout=int(os.getenv("DEEPSEEK_TIMEOUT", "90")),
            deepseek_max_retries=int(os.getenv("DEEPSEEK_MAX_RETRIES", "3")),
            deepseek_max_concurrency=int(os.getenv("DEEPSEEK_MAX_CONCURRENCY", "7")),
            glm_api_key=os.getenv("GLM_API_KEY"),
            glm_base_url=os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            glm_model=os.getenv("GLM_MODEL", "glm-4.7"),
            glm_timeout=int(os.getenv("GLM_TIMEOUT", "90")),
            glm_max_retries=int(os.getenv("GLM_MAX_RETRIES", "5")),
            glm_max_concurrency=int(os.getenv("GLM_MAX_CONCURRENCY", "2")),
            glm_min_request_interval=float(os.getenv("GLM_MIN_REQUEST_INTERVAL", "1.2")),
            glm_rate_limit_cooldown=float(os.getenv("GLM_RATE_LIMIT_COOLDOWN", "8.0")),
            sync_published_after=os.getenv("SYNC_PUBLISHED_AFTER"),
            sync_scan_limit=int(os.getenv("SYNC_SCAN_LIMIT", "100")),
        )

    def validate_for_sync(self) -> None:
        """Validate configuration for Notion sync."""
        if not self.notion_api_key:
            raise ValueError("NOTION_API_KEY is required for Notion sync")
        if not self.notion_database_id:
            raise ValueError("NOTION_DATABASE_ID is required for Notion sync")
