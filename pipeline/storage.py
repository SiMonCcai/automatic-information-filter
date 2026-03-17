"""
SQLite storage layer for the RSS pipeline.
Manages feeds, articles, and sync jobs.
"""

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from pathlib import Path


@dataclass
class Feed:
    """RSS feed entity."""
    id: int
    name: str
    url: str
    enabled: bool
    created_at: str
    last_fetched_at: Optional[str] = None
    fetch_error: Optional[str] = None
    default_author: Optional[str] = None


@dataclass
class Article:
    """Article entity."""
    id: int
    feed_id: int
    title: str
    url: str
    author: Optional[str]
    content_raw: Optional[str]
    content_text: Optional[str]
    published_at: Optional[str]
    fetched_at: str
    fingerprint: Optional[str]
    synced_at: Optional[str] = None
    notion_page_id: Optional[str] = None


@dataclass
class SyncJob:
    """Sync job entity."""
    id: int
    started_at: str
    finished_at: Optional[str]
    status: str
    articles_synced: int
    error_message: Optional[str]


class Storage:
    """SQLite storage manager with thread-safe operations."""

    def __init__(self, db_path: str = "pipeline.db"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @contextmanager
    def transaction(self):
        """Context manager for transactions."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self.transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS feeds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_fetched_at TEXT,
                    fetch_error TEXT,
                    default_author TEXT
                )
            """)

            # Migration: add default_author column if not exists
            cursor = conn.execute("PRAGMA table_info(feeds)")
            columns = [row[1] for row in cursor.fetchall()]
            if "default_author" not in columns:
                conn.execute("ALTER TABLE feeds ADD COLUMN default_author TEXT")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS articles_raw (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    author TEXT,
                    content_raw TEXT,
                    content_text TEXT,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                    fingerprint TEXT,
                    synced_at TEXT,
                    notion_page_id TEXT,
                    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    articles_synced INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                )
            """)

            # Indexes for common queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_articles_feed_fetched
                ON articles_raw(feed_id, fetched_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_articles_synced
                ON articles_raw(synced_at) WHERE synced_at IS NULL
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_articles_fingerprint
                ON articles_raw(fingerprint) WHERE fingerprint IS NOT NULL
            """)

    # Feed operations

    def add_feed(self, name: str, url: str) -> Feed:
        """Add a new feed."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO feeds (name, url, enabled) VALUES (?, ?, 1)",
                (name, url)
            )
            return self.get_feed(cursor.lastrowid)

    def get_feed(self, feed_id: int) -> Optional[Feed]:
        """Get feed by ID."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,))
        row = cursor.fetchone()
        if row:
            return Feed(**dict(row))
        return None

    def get_feed_by_url(self, url: str) -> Optional[Feed]:
        """Get feed by URL."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM feeds WHERE url = ?", (url,))
        row = cursor.fetchone()
        if row:
            return Feed(**dict(row))
        return None

    def list_feeds(self, enabled_only: bool = False) -> List[Feed]:
        """List all feeds."""
        conn = self._get_conn()
        if enabled_only:
            cursor = conn.execute("SELECT * FROM feeds WHERE enabled = 1 ORDER BY name")
        else:
            cursor = conn.execute("SELECT * FROM feeds ORDER BY name")
        return [Feed(**dict(row)) for row in cursor.fetchall()]

    def update_feed_fetch(self, feed_id: int, error: Optional[str] = None) -> None:
        """Update feed fetch timestamp and error state."""
        with self.transaction() as conn:
            conn.execute(
                """UPDATE feeds
                   SET last_fetched_at = datetime('now'), fetch_error = ?
                   WHERE id = ?""",
                (error, feed_id)
            )

    def set_feed_enabled(self, feed_id: int, enabled: bool) -> None:
        """Enable or disable a feed."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE feeds SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, feed_id)
            )

    def update_feed_default_author(self, feed_id: int, default_author: Optional[str]) -> None:
        """Update default author for a feed."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE feeds SET default_author = ? WHERE id = ?",
                (default_author, feed_id)
            )

    def delete_feed(self, feed_id: int) -> None:
        """Delete a feed (cascades to articles)."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))

    def get_unsynced_count(self) -> int:
        """Get count of unsynced articles."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM articles_raw WHERE synced_at IS NULL"
        )
        return cursor.fetchone()[0]

    def list_sync_jobs(self, limit: int = 20) -> List[SyncJob]:
        """List recent sync jobs."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM sync_jobs ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return [SyncJob(**dict(row)) for row in cursor.fetchall()]

    # Article operations

    def add_article(
        self,
        feed_id: int,
        title: str,
        url: str,
        author: Optional[str],
        content_raw: Optional[str],
        content_text: Optional[str],
        published_at: Optional[str],
        fingerprint: Optional[str],
    ) -> Optional[Article]:
        """Add article, returns None if URL already exists for feed."""
        try:
            with self.transaction() as conn:
                cursor = conn.execute("""
                    INSERT INTO articles_raw
                    (feed_id, title, url, author, content_raw, content_text,
                     published_at, fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (feed_id, title, url, author, content_raw, content_text,
                      published_at, fingerprint))
                return self.get_article(cursor.lastrowid)
        except sqlite3.IntegrityError:
            # Duplicate URL for this feed
            return None

    def get_article(self, article_id: int) -> Optional[Article]:
        """Get article by ID."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM articles_raw WHERE id = ?", (article_id,))
        row = cursor.fetchone()
        if row:
            return Article(**dict(row))
        return None

    def get_article_by_fingerprint(self, fingerprint: str) -> Optional[Article]:
        """Get article by fingerprint."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM articles_raw WHERE fingerprint = ? LIMIT 1",
            (fingerprint,)
        )
        row = cursor.fetchone()
        if row:
            return Article(**dict(row))
        return None

    def get_article_by_url(self, url: str) -> Optional[Article]:
        """Get article by URL (globally unique)."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM articles_raw WHERE url = ? LIMIT 1",
            (url,)
        )
        row = cursor.fetchone()
        if row:
            return Article(**dict(row))
        return None

    def get_unsynced_articles(self, limit: int = 100) -> List[Article]:
        """Get articles pending sync."""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT * FROM articles_raw
            WHERE synced_at IS NULL
            ORDER BY fetched_at ASC
            LIMIT ?
        """, (limit,))
        return [Article(**dict(row)) for row in cursor.fetchall()]

    def mark_article_synced(self, article_id: int, notion_page_id: str) -> None:
        """Mark article as synced."""
        with self.transaction() as conn:
            conn.execute("""
                UPDATE articles_raw
                SET synced_at = datetime('now'), notion_page_id = ?
                WHERE id = ?
            """, (notion_page_id, article_id))

    def cleanup_synced_articles(self, keep_days: int = 7) -> int:
        """Delete synced articles older than keep_days days.

        Keeps unsynced rows intact to avoid data loss before Notion sync.
        Returns number of deleted rows.
        """
        with self.transaction() as conn:
            cursor = conn.execute("""
                DELETE FROM articles_raw
                WHERE synced_at IS NOT NULL
                  AND fetched_at < datetime('now', ?)
            """, (f'-{keep_days} days',))
            return cursor.rowcount

    # Sync job operations

    def create_sync_job(self) -> SyncJob:
        """Create a new sync job."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_jobs (status, articles_synced) VALUES ('running', 0)"
            )
            return self.get_sync_job(cursor.lastrowid)

    def get_sync_job(self, job_id: int) -> Optional[SyncJob]:
        """Get sync job by ID."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        if row:
            return SyncJob(**dict(row))
        return None

    def finish_sync_job(self, job_id: int, status: str, articles_synced: int,
                        error: Optional[str] = None) -> None:
        """Finish a sync job."""
        with self.transaction() as conn:
            conn.execute("""
                UPDATE sync_jobs
                SET finished_at = datetime('now'),
                    status = ?,
                    articles_synced = ?,
                    error_message = ?
                WHERE id = ?
            """, (status, articles_synced, error, job_id))

    def close(self) -> None:
        """Close database connection."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")
