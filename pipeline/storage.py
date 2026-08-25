"""
SQLite storage layer for the RSS pipeline.
Manages feeds, articles, and sync jobs.
"""

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional


SCORING_PROMPT_KEYS = [
    "实用性",
    "客观性",
    "是否营销内容",
    "有趣性",
    "独特性",
    "信息密度",
]
AI_META_FIELDS = ["分类", "摘要", "金句"]
AI_ALL_FIELDS = SCORING_PROMPT_KEYS + AI_META_FIELDS
AI_COMBINED_PROMPT_KEY = "ai_combined_prompt::分类_摘要_金句"
_WINNER_UNSET = object()
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
class FeedKeywordRule:
    """Per-feed discard keyword rule."""
    id: int
    feed_id: int
    target_field: str
    keyword: str
    enabled: bool
    created_at: str
    updated_at: str


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


@dataclass
class AIQueueClaim:
    token: Optional[str]
    article_ids: List[int]


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
            self._local.conn.execute("PRAGMA foreign_keys = ON")
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

            # Event records intentionally do not reference articles_raw: article cleanup
            # must never erase or invalidate event history.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    notion_page_id TEXT UNIQUE,
                    current_winner_article_id INTEGER,
                    current_winner_score_total REAL,
                    current_winner_score_count INTEGER NOT NULL DEFAULT 0,
                    member_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                    replacement_count INTEGER NOT NULL DEFAULT 0,
                    embedding_model TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_members (
                    event_id TEXT NOT NULL,
                    article_id INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source TEXT,
                    embedding BLOB NOT NULL,
                    similarity REAL,
                    score_total REAL,
                    score_count INTEGER NOT NULL DEFAULT 0,
                    candidate_status TEXT NOT NULL DEFAULT 'candidate',
                    reading_reset_done BOOLEAN NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (event_id, article_id),
                    FOREIGN KEY (event_id) REFERENCES events(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_replacements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    old_article_id INTEGER,
                    new_article_id INTEGER NOT NULL,
                    old_score_total REAL,
                    new_score_total REAL NOT NULL,
                    operation_id TEXT UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (event_id) REFERENCES events(id)
                )
            """)
            event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            for name, definition in {
                "state": "TEXT NOT NULL DEFAULT 'presentation_pending'",
                "revision": "INTEGER NOT NULL DEFAULT 0",
                "presented_revision": "INTEGER NOT NULL DEFAULT -1",
                "pending_reset_revision": "INTEGER NOT NULL DEFAULT 0",
                "reset_done_revision": "INTEGER NOT NULL DEFAULT -1",
                "winner_operation_id": "TEXT",
            }.items():
                if name not in event_columns:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
            member_columns = {row[1] for row in conn.execute("PRAGMA table_info(event_members)")}
            for name, definition in {
                "member_revision": "INTEGER NOT NULL DEFAULT 0",
                "notion_page_id": "TEXT",
                "synced_at": "TEXT",
            }.items():
                if name not in member_columns:
                    conn.execute(f"ALTER TABLE event_members ADD COLUMN {name} {definition}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS dedup_tombstones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    fingerprint TEXT,
                    title TEXT,
                    published_at TEXT,
                    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS article_content_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    content_text TEXT,
                    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS feed_keyword_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id INTEGER NOT NULL,
                    target_field TEXT NOT NULL CHECK(target_field IN ('title', 'content')),
                    keyword TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS article_discard_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_id INTEGER NOT NULL,
                    feed_name TEXT,
                    article_title TEXT,
                    article_url TEXT,
                    matched_field TEXT NOT NULL CHECK(matched_field IN ('title', 'content')),
                    matched_keyword TEXT NOT NULL,
                    discard_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
                )
            """)

            # Seed scoring prompt keys for admin editing
            for key in SCORING_PROMPT_KEYS:
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, '')",
                    (f'scoring_prompt::{key}',)
                )
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, '')",
                (AI_COMBINED_PROMPT_KEY,)
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS article_ai_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    notion_page_id TEXT,
                    field_name TEXT NOT NULL,
                    request_group TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    push_status TEXT NOT NULL DEFAULT 'pending',
                    value_text TEXT,
                    raw_response TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    push_attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    push_error TEXT,
                    last_requested_at TEXT,
                    completed_at TEXT,
                    pushed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(article_id, field_name),
                    FOREIGN KEY (article_id) REFERENCES articles_raw(id) ON DELETE CASCADE
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_enrichment_queue (
                    article_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL DEFAULT (datetime('now')),
                    claimed_at TEXT,
                    claim_token TEXT,
                    completed_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (article_id) REFERENCES articles_raw(id) ON DELETE CASCADE
                )
            """)
            queue_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(ai_enrichment_queue)").fetchall()
            }
            if "claim_token" not in queue_columns:
                conn.execute("ALTER TABLE ai_enrichment_queue ADD COLUMN claim_token TEXT")
            queue_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(ai_enrichment_queue)")
            }
            for name, definition in {
                "phase": "TEXT NOT NULL DEFAULT 'full'",
                "mode": "TEXT NOT NULL DEFAULT 'regular'",
                "event_id": "TEXT",
                "operation_id": "TEXT",
            }.items():
                if name not in queue_columns:
                    conn.execute(f"ALTER TABLE ai_enrichment_queue ADD COLUMN {name} {definition}")

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
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tombstones_fingerprint
                ON dedup_tombstones(fingerprint) WHERE fingerprint IS NOT NULL
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_article_content_archive_last_seen
                ON article_content_archive(last_seen_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_results_article
                ON article_ai_results(article_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_results_status
                ON article_ai_results(status, push_status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_queue_status_available
                ON ai_enrichment_queue(status, available_at, created_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_feed_keyword_rules_feed
                ON feed_keyword_rules(feed_id, target_field, enabled)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_article_discard_logs_feed
                ON article_discard_logs(feed_id, created_at DESC)
            """)

            conn.execute("""
                INSERT INTO article_content_archive (
                    url, title, content_text, first_seen_at, last_seen_at
                )
                SELECT
                    url,
                    title,
                    content_text,
                    COALESCE(synced_at, fetched_at, datetime('now')),
                    COALESCE(synced_at, fetched_at, datetime('now'))
                FROM articles_raw
                WHERE 1 = 1
                ON CONFLICT(url) DO UPDATE SET
                    title = COALESCE(excluded.title, article_content_archive.title),
                    content_text = COALESCE(excluded.content_text, article_content_archive.content_text),
                    last_seen_at = excluded.last_seen_at
            """)

            conn.execute("""
                INSERT INTO dedup_tombstones (
                    url, fingerprint, title, published_at, first_seen_at, last_seen_at
                )
                SELECT
                    url,
                    fingerprint,
                    title,
                    published_at,
                    COALESCE(synced_at, fetched_at, datetime('now')),
                    COALESCE(synced_at, fetched_at, datetime('now'))
                FROM articles_raw
                WHERE synced_at IS NOT NULL
                ON CONFLICT(url) DO UPDATE SET
                    fingerprint = COALESCE(excluded.fingerprint, dedup_tombstones.fingerprint),
                    title = COALESCE(excluded.title, dedup_tombstones.title),
                    published_at = COALESCE(excluded.published_at, dedup_tombstones.published_at),
                    last_seen_at = excluded.last_seen_at
            """)

            # One-time safety backfill for pages synced before the queue existed
            # but never handed to the AI enrichment layer.
            conn.execute("""
                INSERT OR IGNORE INTO ai_enrichment_queue (article_id)
                SELECT article.id
                FROM articles_raw AS article
                WHERE article.notion_page_id IS NOT NULL
                  AND article.notion_page_id != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM article_ai_results AS result
                      WHERE result.article_id = article.id
                  )
            """)

    # Event operations

    def create_event(self, embedding_model: str, event_id: Optional[str] = None) -> str:
        """Create a durable event and return its stable identifier."""
        stable_id = event_id or str(uuid.uuid4())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events (id, embedding_model) VALUES (?, ?)",
                (stable_id, embedding_model),
            )
        return stable_id

    def append_event_member(
        self,
        *,
        event_id: str,
        article_id: int,
        title: str,
        url: str,
        source: Optional[str],
        embedding: bytes,
        similarity: Optional[float] = None,
        candidate_status: str = "candidate",
    ) -> bool:
        """Append an article once, preserving the first recorded event membership."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_members (
                    event_id, article_id, title, url, source, embedding,
                    similarity, candidate_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    article_id,
                    title,
                    url,
                    source,
                    sqlite3.Binary(embedding),
                    similarity,
                    candidate_status,
                ),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                """
                UPDATE events
                SET member_count = member_count + 1,
                    last_seen_at = datetime('now'),
                    revision = revision + 1,
                    pending_reset_revision = revision + 1,
                    state = 'presentation_pending'
                WHERE id = ?
                """,
                (event_id,),
            )
            conn.execute(
                "UPDATE event_members SET member_revision = (SELECT revision FROM events WHERE id = ?) WHERE article_id = ?",
                (event_id, article_id),
            )
            return True

    def get_recent_event_members(
        self, embedding_model: str, window_days: int = 7
    ) -> List[dict]:
        """Return event members in the matching model's recent time window."""
        cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        rows = self._get_conn().execute(
            """
            SELECT member.*
            FROM event_members AS member
            JOIN events AS event ON event.id = member.event_id
            WHERE event.embedding_model = ? AND member.created_at >= ?
            ORDER BY member.created_at ASC, member.article_id ASC
            """,
            (embedding_model, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_event_page_id(self, event_id: str, notion_page_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET notion_page_id = ? WHERE id = ?",
                (notion_page_id, event_id),
            )

    def mark_event_canonical_synced(self, event_id: str, notion_page_id: str) -> int:
        """Atomically attach one canonical page to the event and every live member."""
        with self.transaction() as conn:
            conn.execute("UPDATE events SET notion_page_id = ? WHERE id = ?", (notion_page_id, event_id))
            members = conn.execute(
                "SELECT article_id FROM event_members WHERE event_id = ?", (event_id,)
            ).fetchall()
            article_ids = [int(row[0]) for row in members]
            conn.execute(
                "UPDATE event_members SET notion_page_id=?, synced_at=datetime('now'), updated_at=datetime('now') WHERE event_id=?",
                (notion_page_id, event_id),
            )
            if article_ids:
                placeholders = ",".join("?" for _ in article_ids)
                conn.execute(
                    f"UPDATE articles_raw SET notion_page_id=?, synced_at=datetime('now') WHERE id IN ({placeholders})",
                    [notion_page_id, *article_ids],
                )
            return len(article_ids)

    def claim_pending_event_presentations(self, limit: int = 100) -> List[dict]:
        """List durable page-presentation effects; claiming is intentionally retryable."""
        rows = self._get_conn().execute(
            """
            SELECT * FROM events
            WHERE notion_page_id IS NOT NULL
              AND (presented_revision < revision OR reset_done_revision < pending_reset_revision)
            ORDER BY last_seen_at, id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        claims = []
        for row in rows:
            item = dict(row)
            item["event_id"] = item["id"]
            item["reset_reading"] = item["reset_done_revision"] < item["pending_reset_revision"]
            item["members"] = self.list_event_members(item["id"])
            claims.append(item)
        return claims

    def mark_event_presented(self, event_id: str, revision: int, *, reset_succeeded: bool) -> bool:
        """Ack a rendered revision and only ack reading reset after Notion succeeded."""
        with self.transaction() as conn:
            event = conn.execute("SELECT revision FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None or revision > int(event["revision"]):
                return False
            cursor = conn.execute(
                """
                UPDATE events
                SET presented_revision = MAX(presented_revision, ?),
                    reset_done_revision = CASE WHEN ? THEN MAX(reset_done_revision, ?) ELSE reset_done_revision END,
                    state = CASE
                        WHEN ? AND revision = ? THEN 'score_pending'
                        ELSE state
                    END
                WHERE id = ?
                """,
                (revision, int(reset_succeeded), revision, int(reset_succeeded), revision, event_id),
            )
            if reset_succeeded:
                conn.execute(
                    "UPDATE event_members SET reading_reset_done=1 WHERE event_id=? AND member_revision <= ?",
                    (event_id, revision),
                )
            return cursor.rowcount == 1

    def get_event(self, event_id: str) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_event_members(self, event_id: str) -> List[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM event_members WHERE event_id = ? ORDER BY created_at, article_id",
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_event_id_for_article(self, article_id: int) -> Optional[str]:
        row = self._get_conn().execute(
            "SELECT event_id FROM event_members WHERE article_id=?", (article_id,)
        ).fetchone()
        return str(row["event_id"]) if row else None

    def list_event_replacements(self, event_id: str) -> List[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM event_replacements WHERE event_id=? ORDER BY id",
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_event_member_scores(
        self, article_id: int, *, score_total: float, score_count: int
    ) -> None:
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT event_id, score_total, score_count FROM event_members WHERE article_id = ?",
                (article_id,),
            ).fetchone()
            if current is None:
                return
            if current["score_total"] == score_total and current["score_count"] == score_count:
                return
            conn.execute(
                """
                UPDATE event_members
                SET score_total = ?, score_count = ?, updated_at = datetime('now')
                WHERE article_id = ?
                """,
                (score_total, score_count, article_id),
            )
            # Score visibility is a presentation change, not a new-event alert:
            # increment revision but leave pending_reset_revision untouched.
            conn.execute(
                "UPDATE events SET revision=revision+1 WHERE id=?",
                (current["event_id"],),
            )

    def aggregate_event_member_scores(self, article_id: int) -> Optional[dict]:
        """Return a complete validated six-score snapshot, never a partial total."""
        rows = self.get_ai_results_for_article(article_id)
        values: dict[str, int] = {}
        for field in SCORING_PROMPT_KEYS:
            row = rows.get(field)
            if not row or row.get("status") != "completed":
                return None
            raw = str(row.get("value_text") or "").strip()
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None
            if str(value) != raw or not 1 <= value <= 5:
                return None
            values[field] = value
        total = sum(values.values())
        self.set_event_member_scores(article_id, score_total=total, score_count=len(values))
        return {"values": values, "score_total": total, "score_count": len(values)}

    def decide_event_candidate(self, event_id: str, article_id: int, *, margin: int = 2) -> dict:
        """Persist the score-phase decision; representative replacement remains two-phase."""
        snapshot = self.aggregate_event_member_scores(article_id)
        if snapshot is None:
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE event_members SET candidate_status='blocked', updated_at=datetime('now') WHERE event_id=? AND article_id=?",
                    (event_id, article_id),
                )
            return {"decision": "blocked"}
        event = self.get_event(event_id)
        if event is None:
            return {"decision": "missing_event"}
        winner_id = event["current_winner_article_id"]
        total = snapshot["score_total"]
        if winner_id is None:
            initial = self._get_conn().execute(
                "SELECT article_id FROM event_members WHERE event_id=? ORDER BY created_at, article_id LIMIT 1",
                (event_id,),
            ).fetchone()
            if initial is not None and int(initial["article_id"]) != article_id:
                self.defer_event_ai_queue(article_id, "waiting_initial_winner")
                with self.transaction() as conn:
                    conn.execute(
                        "UPDATE event_members SET candidate_status='blocked', updated_at=datetime('now') WHERE event_id=? AND article_id=?",
                        (event_id, article_id),
                    )
                return {"decision": "blocked", "reason": "waiting_initial_winner", **snapshot}
            operation_id = f"initial:{event_id}:{article_id}:{total}"
            changed = self.set_event_winner(
                event_id, article_id, score_total=total, score_count=6,
                expected_old_winner=None, operation_id=operation_id,
            )
            return {"decision": "initial_winner" if changed else "blocked", **snapshot, "operation_id": operation_id}
        winner_total = float(event["current_winner_score_total"] or 0)
        if article_id == winner_id:
            return {"decision": "winner", **snapshot}
        if total < winner_total + margin:
            self.mark_ai_fields_skipped(
                article_id, event.get("notion_page_id") or "", AI_META_FIELDS,
                "meta", "event_loser", max_attempts=0,
            )
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE event_members SET candidate_status='loser', updated_at=datetime('now') WHERE event_id=? AND article_id=?",
                    (event_id, article_id),
                )
            return {"decision": "loser", **snapshot}
        operation_id = f"replace:{event_id}:{winner_id}:{article_id}:{total}"
        with self.transaction() as conn:
            conn.execute(
                "UPDATE events SET state='replacement_pending' WHERE id=?",
                (event_id,),
            )
            conn.execute(
                "UPDATE event_members SET candidate_status='replacement_pending', updated_at=datetime('now') WHERE event_id=? AND article_id=?",
                (event_id, article_id),
            )
        return {
            "decision": "replacement_pending", "expected_old_winner": winner_id,
            "operation_id": operation_id, **snapshot,
        }

    def set_event_winner(
        self,
        event_id: str,
        article_id: int,
        *,
        score_total: float,
        score_count: int,
        replacement: bool = False,
        expected_old_winner: object = _WINNER_UNSET,
        operation_id: Optional[str] = None,
    ) -> bool:
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT current_winner_article_id, current_winner_score_total, winner_operation_id FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if current is None:
                return False
            if operation_id and current["winner_operation_id"] == operation_id:
                return True
            if expected_old_winner is not _WINNER_UNSET and current["current_winner_article_id"] != expected_old_winner:
                return False
            cursor = conn.execute(
                """
                UPDATE events
                SET current_winner_article_id = ?,
                    current_winner_score_total = ?,
                    current_winner_score_count = ?,
                    replacement_count = replacement_count + ?,
                    winner_operation_id = ?,
                    state = 'meta_pending'
                WHERE id = ?
                """,
                (article_id, score_total, score_count, int(replacement), operation_id, event_id),
            )
            conn.execute(
                """
                UPDATE event_members
                SET candidate_status = CASE
                        WHEN article_id = ? THEN 'winner'
                        WHEN candidate_status = 'winner' THEN 'replaced'
                        ELSE candidate_status
                    END,
                    updated_at = datetime('now')
                WHERE event_id = ?
                """,
                (article_id, event_id),
            )
            if replacement and cursor.rowcount == 1:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO event_replacements (
                        event_id, old_article_id, new_article_id,
                        old_score_total, new_score_total, operation_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        current["current_winner_article_id"],
                        article_id,
                        current["current_winner_score_total"],
                        score_total,
                        operation_id,
                    ),
                )
            return cursor.rowcount == 1

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

    def list_feed_keyword_rules(self, feed_id: int, enabled_only: bool = False) -> List[FeedKeywordRule]:
        """List keyword rules for a feed."""
        conn = self._get_conn()
        if enabled_only:
            cursor = conn.execute(
                """
                SELECT * FROM feed_keyword_rules
                WHERE feed_id = ? AND enabled = 1
                ORDER BY target_field, id ASC
                """,
                (feed_id,),
            )
        else:
            cursor = conn.execute(
                """
                SELECT * FROM feed_keyword_rules
                WHERE feed_id = ?
                ORDER BY target_field, id ASC
                """,
                (feed_id,),
            )
        return [FeedKeywordRule(**dict(row)) for row in cursor.fetchall()]

    def get_feed_keyword_filters(self, feed_id: int, enabled_only: bool = True) -> dict[str, list[str]]:
        """Return feed keyword rules grouped by title/content."""
        grouped = {"title_keywords": [], "content_keywords": []}
        for rule in self.list_feed_keyword_rules(feed_id, enabled_only=enabled_only):
            key = f"{rule.target_field}_keywords"
            if key in grouped:
                grouped[key].append(rule.keyword)
        return grouped

    def replace_feed_keyword_rules(
        self,
        feed_id: int,
        title_keywords: List[str],
        content_keywords: List[str],
    ) -> None:
        """Replace all keyword rules for a feed."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM feed_keyword_rules WHERE feed_id = ?", (feed_id,))
            for target_field, keywords in (("title", title_keywords), ("content", content_keywords)):
                for keyword in keywords:
                    conn.execute(
                        """
                        INSERT INTO feed_keyword_rules (feed_id, target_field, keyword, enabled)
                        VALUES (?, ?, ?, 1)
                        """,
                        (feed_id, target_field, keyword),
                    )

    def add_article_discard_log(
        self,
        feed_id: int,
        feed_name: Optional[str],
        article_title: Optional[str],
        article_url: Optional[str],
        matched_field: str,
        matched_keyword: str,
        discard_reason: str = "keyword_filter",
    ) -> None:
        """Persist discard-log rows for articles discarded before main ingestion."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO article_discard_logs (
                    feed_id, feed_name, article_title, article_url,
                    matched_field, matched_keyword, discard_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (feed_id, feed_name, article_title, article_url, matched_field, matched_keyword, discard_reason),
            )

    def list_article_discard_logs(self, feed_id: int, limit: int = 20) -> List[dict]:
        """List recent discard logs for a feed."""
        conn = self._get_conn()
        cursor = conn.execute(
            """
            SELECT * FROM article_discard_logs
            WHERE feed_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (feed_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

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
                conn.execute("""
                    INSERT INTO article_content_archive (
                        url, title, content_text, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, datetime('now'), datetime('now'))
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        content_text = COALESCE(excluded.content_text, article_content_archive.content_text),
                        last_seen_at = excluded.last_seen_at
                """, (url, title, content_text))
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

    def get_dedup_tombstone_by_url(self, url: str) -> Optional[sqlite3.Row]:
        """Get dedup tombstone by normalized URL."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM dedup_tombstones WHERE url = ? LIMIT 1",
            (url,)
        )
        return cursor.fetchone()

    def get_dedup_tombstone_by_fingerprint(self, fingerprint: str) -> Optional[sqlite3.Row]:
        """Get dedup tombstone by fingerprint."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM dedup_tombstones WHERE fingerprint = ? LIMIT 1",
            (fingerprint,)
        )
        return cursor.fetchone()

    def upsert_dedup_tombstone(
        self,
        url: str,
        fingerprint: Optional[str],
        title: Optional[str],
        published_at: Optional[str],
        seen_at: Optional[str] = None,
    ) -> None:
        """Persist lightweight dedup history for synced articles."""
        seen_value = seen_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO dedup_tombstones (
                    url, fingerprint, title, published_at, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    fingerprint = COALESCE(excluded.fingerprint, dedup_tombstones.fingerprint),
                    title = COALESCE(excluded.title, dedup_tombstones.title),
                    published_at = COALESCE(excluded.published_at, dedup_tombstones.published_at),
                    last_seen_at = excluded.last_seen_at
                """,
                (url, fingerprint, title, published_at, seen_value, seen_value),
            )

    def get_unsynced_articles(self, limit: int = 100, *, event_mode: bool = True) -> List[Article]:
        """Get legacy unsynced rows or one canonical row per event when enabled."""
        conn = self._get_conn()
        if not event_mode:
            rows = conn.execute(
                "SELECT * FROM articles_raw WHERE synced_at IS NULL ORDER BY fetched_at, id LIMIT ?",
                (limit,),
            ).fetchall()
            return [Article(**dict(row)) for row in rows]
        cursor = conn.execute("""
            SELECT article.* FROM articles_raw AS article
            LEFT JOIN event_members AS member ON member.article_id = article.id
            WHERE article.synced_at IS NULL
              AND (
                member.article_id IS NULL OR member.article_id = (
                    SELECT first.article_id FROM event_members AS first
                    WHERE first.event_id = member.event_id
                    ORDER BY first.created_at, first.article_id LIMIT 1
                )
              )
            ORDER BY article.fetched_at ASC, article.id ASC
            LIMIT ?
        """, (limit,))
        return [Article(**dict(row)) for row in cursor.fetchall()]

    def get_unclustered_unsynced_articles(self, limit: int = 1000) -> List[Article]:
        rows = self._get_conn().execute(
            """
            SELECT article.* FROM articles_raw AS article
            WHERE article.synced_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM event_members WHERE article_id = article.id)
            ORDER BY article.fetched_at, article.id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [Article(**dict(row)) for row in rows]

    def mark_article_synced(self, article_id: int, notion_page_id: str) -> None:
        """Mark article as synced."""
        with self.transaction() as conn:
            conn.execute("""
                UPDATE articles_raw
                SET synced_at = datetime('now'), notion_page_id = ?
                WHERE id = ?
            """, (notion_page_id, article_id))
            conn.execute("""
                INSERT INTO dedup_tombstones (
                    url, fingerprint, title, published_at, first_seen_at, last_seen_at
                )
                SELECT
                    url,
                    fingerprint,
                    title,
                    published_at,
                    COALESCE(synced_at, fetched_at, datetime('now')),
                    COALESCE(synced_at, fetched_at, datetime('now'))
                FROM articles_raw
                WHERE id = ?
                ON CONFLICT(url) DO UPDATE SET
                    fingerprint = COALESCE(excluded.fingerprint, dedup_tombstones.fingerprint),
                    title = COALESCE(excluded.title, dedup_tombstones.title),
                    published_at = COALESCE(excluded.published_at, dedup_tombstones.published_at),
                    last_seen_at = excluded.last_seen_at
            """, (article_id,))

    def cleanup_synced_articles(self, keep_days: int = 7) -> int:
        """Delete synced articles older than keep_days days.

        Keeps unsynced rows intact to avoid data loss before Notion sync.
        Returns number of deleted rows.
        """
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO article_content_archive (
                    url, title, content_text, first_seen_at, last_seen_at
                )
                SELECT
                    url,
                    title,
                    content_text,
                    COALESCE(synced_at, fetched_at, datetime('now')),
                    COALESCE(synced_at, fetched_at, datetime('now'))
                FROM articles_raw
                WHERE synced_at IS NOT NULL
                  AND fetched_at < datetime('now', ?)
                ON CONFLICT(url) DO UPDATE SET
                    title = COALESCE(excluded.title, article_content_archive.title),
                    content_text = COALESCE(excluded.content_text, article_content_archive.content_text),
                    last_seen_at = excluded.last_seen_at
            """, (f'-{keep_days} days',))
            conn.execute("""
                INSERT INTO dedup_tombstones (
                    url, fingerprint, title, published_at, first_seen_at, last_seen_at
                )
                SELECT
                    url,
                    fingerprint,
                    title,
                    published_at,
                    COALESCE(synced_at, fetched_at, datetime('now')),
                    COALESCE(synced_at, fetched_at, datetime('now'))
                FROM articles_raw
                WHERE synced_at IS NOT NULL
                  AND fetched_at < datetime('now', ?)
                ON CONFLICT(url) DO UPDATE SET
                    fingerprint = COALESCE(excluded.fingerprint, dedup_tombstones.fingerprint),
                    title = COALESCE(excluded.title, dedup_tombstones.title),
                    published_at = COALESCE(excluded.published_at, dedup_tombstones.published_at),
                    last_seen_at = excluded.last_seen_at
            """, (f'-{keep_days} days',))
            conn.execute("""
                DELETE FROM article_ai_results
                WHERE article_id IN (
                    SELECT article.id FROM articles_raw AS article
                    WHERE article.synced_at IS NOT NULL
                      AND article.fetched_at < datetime('now', ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM ai_enrichment_queue AS queue
                          WHERE queue.article_id = article.id
                            AND queue.status IN ('pending', 'processing')
                      )
                )
            """, (f'-{keep_days} days',))
            cursor = conn.execute("""
                DELETE FROM articles_raw
                WHERE synced_at IS NOT NULL
                  AND fetched_at < datetime('now', ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM ai_enrichment_queue AS queue
                      WHERE queue.article_id = articles_raw.id
                        AND queue.status IN ('pending', 'processing')
                  )
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

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get setting value by key."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Insert or update a setting value."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now')
                """,
                (key, value),
            )

    def get_scoring_prompts(self) -> dict[str, str]:
        """Return configured scoring prompts keyed by dimension."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'scoring_prompt::%' ORDER BY key"
        )
        prompts = {row['key'].split('::', 1)[1]: row['value'] for row in cursor.fetchall()}
        for key in SCORING_PROMPT_KEYS:
            prompts.setdefault(key, '')
        return prompts

    def set_scoring_prompts(self, prompts: dict[str, str]) -> None:
        """Persist scoring prompts for known dimensions."""
        with self.transaction() as conn:
            for key in SCORING_PROMPT_KEYS:
                conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = datetime('now')
                    """,
                    (f'scoring_prompt::{key}', (prompts.get(key) or '').strip()),
                )

    def get_combined_prompt(self) -> str:
        return self.get_setting(AI_COMBINED_PROMPT_KEY, "") or ""

    def set_combined_prompt(self, prompt: str) -> None:
        self.set_setting(AI_COMBINED_PROMPT_KEY, (prompt or "").strip())

    def get_ai_prompt_config(self) -> dict[str, object]:
        return {
            "score_dimensions": list(SCORING_PROMPT_KEYS),
            "score_prompts": self.get_scoring_prompts(),
            "meta_fields": list(AI_META_FIELDS),
            "combined_prompt": self.get_combined_prompt(),
        }

    def list_recent_synced_articles(self, limit: int = 100) -> List[Article]:
        conn = self._get_conn()
        cursor = conn.execute(
            """
            SELECT * FROM articles_raw
            WHERE notion_page_id IS NOT NULL AND notion_page_id != ''
            ORDER BY datetime(synced_at) ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [Article(**dict(row)) for row in cursor.fetchall()]

    def enqueue_ai_articles(self, article_ids: List[int]) -> int:
        """Persist newly synced articles for deferred AI enrichment."""
        inserted = 0
        with self.transaction() as conn:
            for article_id in dict.fromkeys(article_ids):
                event = conn.execute(
                    "SELECT event_id FROM event_members WHERE article_id=?", (article_id,)
                ).fetchone()
                event_id = event["event_id"] if event else None
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO ai_enrichment_queue
                        (article_id, phase, mode, event_id, operation_id)
                    SELECT id, ?, ?, ?, ? FROM articles_raw
                    WHERE id = ? AND notion_page_id IS NOT NULL AND notion_page_id != ''
                    """,
                    (
                        "score" if event_id else "full",
                        "event" if event_id else "regular",
                        event_id,
                        uuid.uuid4().hex,
                        article_id,
                    ),
                )
                inserted += max(cursor.rowcount, 0)
        return inserted

    def advance_event_ai_queue(self, article_id: int, phase: str, operation_id: str) -> bool:
        """Durably advance an event member between score and meta phases."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE ai_enrichment_queue
                SET phase=?, operation_id=?, status='pending', available_at=datetime('now'),
                    claimed_at=NULL, claim_token=NULL, completed_at=NULL, updated_at=datetime('now')
                WHERE article_id=? AND mode='event'
                """,
                (phase, operation_id, article_id),
            )
            return cursor.rowcount == 1

    def mark_event_ai_queue_terminal(self, article_id: int, outcome: str) -> bool:
        """Record a durable event outcome while retaining an active worker claim."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT event_id FROM ai_enrichment_queue WHERE article_id=? AND mode='event'",
                (article_id,),
            ).fetchone()
            cursor = conn.execute(
                """
                UPDATE ai_enrichment_queue
                SET phase='terminal', operation_id=?, updated_at=datetime('now')
                WHERE article_id=? AND mode='event'
                """,
                (outcome, article_id),
            )
            if cursor.rowcount == 1 and row and row["event_id"]:
                conn.execute("UPDATE events SET state='active' WHERE id=?", (row["event_id"],))
            return cursor.rowcount == 1

    def defer_event_ai_queue(self, article_id: int, reason: str) -> bool:
        """Release dependency-blocked work without consuming its queue claim."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE ai_enrichment_queue
                SET status='pending',
                    attempt_count=MAX(attempt_count-1, 0),
                    claimed_at=NULL,
                    claim_token=NULL,
                    available_at=datetime('now'),
                    last_error=?,
                    updated_at=datetime('now')
                WHERE article_id=? AND mode='event' AND status='processing'
                """,
                (reason, article_id),
            )
            return cursor.rowcount == 1

    def claim_ai_queue(self, limit: int, stale_after_minutes: int = 30) -> AIQueueClaim:
        """Atomically claim the oldest available queue entries with an ownership token."""
        limit = max(1, int(limit))
        stale_modifier = f"-{max(1, int(stale_after_minutes))} minutes"
        claim_token = uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE ai_enrichment_queue
                SET status = CASE WHEN attempt_count >= 5 THEN 'failed' ELSE 'pending' END,
                    claimed_at = NULL, claim_token = NULL,
                    available_at = CASE
                        WHEN attempt_count >= 5 THEN available_at
                        ELSE datetime('now', '+10 minutes')
                    END,
                    last_error = COALESCE(last_error, 'stale claim recovered'),
                    updated_at = datetime('now')
                WHERE status = 'processing'
                  AND claimed_at < datetime('now', ?)
                """,
                (stale_modifier,),
            )
            rows = conn.execute(
                """
                SELECT article_id FROM ai_enrichment_queue
                WHERE status = 'pending' AND available_at <= datetime('now')
                ORDER BY datetime(created_at) ASC, article_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            article_ids = [int(row[0]) for row in rows]
            if article_ids:
                placeholders = ",".join("?" for _ in article_ids)
                conn.execute(
                    f"""
                    UPDATE ai_enrichment_queue
                    SET status = 'processing', claimed_at = datetime('now'), claim_token = ?,
                        attempt_count = attempt_count + 1, last_error = NULL,
                        updated_at = datetime('now')
                    WHERE article_id IN ({placeholders}) AND status = 'pending'
                    """,
                    [claim_token, *article_ids],
                )
        return AIQueueClaim(claim_token if article_ids else None, article_ids)

    def renew_ai_queue_claim(self, article_ids: List[int], claim_token: str) -> int:
        """Extend an active claim lease; stale owners cannot renew it."""
        if not article_ids:
            return 0
        placeholders = ",".join("?" for _ in article_ids)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE ai_enrichment_queue
                SET claimed_at = datetime('now'), updated_at = datetime('now')
                WHERE article_id IN ({placeholders})
                  AND status = 'processing' AND claim_token = ?
                """,
                [*article_ids, claim_token],
            )
            return max(cursor.rowcount, 0)

    def complete_ai_queue(self, article_ids: List[int], claim_token: str) -> None:
        self._update_ai_queue_status(article_ids, claim_token, "completed")

    def fail_ai_queue(self, article_ids: List[int], claim_token: str, error: str) -> None:
        self._update_ai_queue_status(article_ids, claim_token, "failed", error)

    def release_ai_queue(self, article_ids: List[int], claim_token: str, error: str) -> None:
        """Return interrupted work with backoff, or fail after five claims."""
        if not article_ids:
            return
        placeholders = ",".join("?" for _ in article_ids)
        with self.transaction() as conn:
            conn.execute(
                f"""
                UPDATE ai_enrichment_queue
                SET status = CASE WHEN attempt_count >= 5 THEN 'failed' ELSE 'pending' END,
                    claimed_at = NULL, claim_token = NULL, completed_at = NULL,
                    available_at = CASE
                        WHEN attempt_count >= 5 THEN available_at
                        ELSE datetime('now', '+10 minutes')
                    END,
                    last_error = ?, updated_at = datetime('now')
                WHERE article_id IN ({placeholders})
                  AND status = 'processing' AND claim_token = ?
                """,
                [error, *article_ids, claim_token],
            )

    def _update_ai_queue_status(
        self,
        article_ids: List[int],
        claim_token: str,
        status: str,
        error: str | None = None,
    ) -> None:
        if not article_ids:
            return
        placeholders = ",".join("?" for _ in article_ids)
        completed_expr = "datetime('now')" if status == "completed" else "NULL"
        with self.transaction() as conn:
            conn.execute(
                f"""
                UPDATE ai_enrichment_queue
                SET status = ?, claimed_at = NULL, claim_token = NULL,
                    completed_at = {completed_expr}, last_error = ?,
                    updated_at = datetime('now')
                WHERE article_id IN ({placeholders})
                  AND status = 'processing' AND claim_token = ?
                """,
                [status, error, *article_ids, claim_token],
            )

    def get_ai_queue_rows(self, article_ids: List[int]) -> dict[int, dict]:
        if not article_ids:
            return {}
        placeholders = ",".join("?" for _ in article_ids)
        rows = self._get_conn().execute(
            f"SELECT * FROM ai_enrichment_queue WHERE article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
        return {int(row["article_id"]): dict(row) for row in rows}

    def get_ai_enrichment_states(self, article_ids: List[int], expected_fields: List[str]) -> dict[int, str]:
        """Return completed, failed, or pending for each queued article."""
        if not article_ids:
            return {}
        placeholders = ",".join("?" for _ in article_ids)
        rows = self._get_conn().execute(
            f"SELECT * FROM article_ai_results WHERE article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
        by_article: dict[int, dict[str, dict]] = {article_id: {} for article_id in article_ids}
        for row in rows:
            by_article[int(row["article_id"])][str(row["field_name"])] = dict(row)

        states: dict[int, str] = {}
        for article_id, field_rows in by_article.items():
            relevant = [field_rows.get(field_name) for field_name in expected_fields]
            if all(
                row is not None
                and (
                    row["status"] == "skipped"
                    or (row["status"] == "completed" and row["push_status"] == "completed")
                )
                for row in relevant
            ):
                states[article_id] = "completed"
            elif any(
                row is not None and (row["status"] == "failed" or row["push_status"] == "failed")
                for row in relevant
            ):
                states[article_id] = "failed"
            else:
                states[article_id] = "pending"
        return states

    def get_ai_results_for_article(self, article_id: int) -> dict[str, dict]:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM article_ai_results WHERE article_id = ? ORDER BY field_name",
            (article_id,),
        )
        return {row["field_name"]: dict(row) for row in cursor.fetchall()}

    def upsert_ai_result_stub(
        self,
        article_id: int,
        notion_page_id: str,
        field_name: str,
        request_group: str,
        max_attempts: int = 3,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO article_ai_results (
                    article_id, notion_page_id, field_name, request_group, max_attempts, updated_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(article_id, field_name) DO UPDATE SET
                    notion_page_id = excluded.notion_page_id,
                    request_group = excluded.request_group,
                    max_attempts = excluded.max_attempts,
                    updated_at = datetime('now')
                """,
                (article_id, notion_page_id, field_name, request_group, max_attempts),
            )

    def mark_ai_fields_processing(
        self,
        article_id: int,
        notion_page_id: str,
        field_names: list[str],
        request_group: str,
        max_attempts: int = 3,
    ) -> None:
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    INSERT INTO article_ai_results (
                        article_id, notion_page_id, field_name, request_group,
                        status, push_status, attempt_count, max_attempts,
                        last_requested_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'processing', 'pending', 1, ?, datetime('now'), datetime('now'))
                    ON CONFLICT(article_id, field_name) DO UPDATE SET
                        notion_page_id = excluded.notion_page_id,
                        request_group = excluded.request_group,
                        status = 'processing',
                        attempt_count = article_ai_results.attempt_count + 1,
                        max_attempts = excluded.max_attempts,
                        last_requested_at = datetime('now'),
                        updated_at = datetime('now'),
                        last_error = NULL
                    """,
                    (article_id, notion_page_id, field_name, request_group, max_attempts),
                )

    def mark_ai_fields_completed(
        self,
        article_id: int,
        values: dict[str, str],
        raw_response: str,
    ) -> None:
        with self.transaction() as conn:
            for field_name, value in values.items():
                conn.execute(
                    """
                    UPDATE article_ai_results
                    SET status = 'completed',
                        value_text = ?,
                        raw_response = ?,
                        completed_at = datetime('now'),
                        updated_at = datetime('now'),
                        last_error = NULL
                    WHERE article_id = ? AND field_name = ?
                    """,
                    (value, raw_response, article_id, field_name),
                )

    def mark_ai_fields_failed(self, article_id: int, field_names: list[str], error: str) -> None:
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    UPDATE article_ai_results
                    SET status = 'failed',
                        last_error = ?,
                        updated_at = datetime('now')
                    WHERE article_id = ? AND field_name = ?
                    """,
                    (error, article_id, field_name),
                )

    def mark_ai_fields_skipped(
        self,
        article_id: int,
        notion_page_id: str,
        field_names: list[str],
        request_group: str,
        reason: str,
        max_attempts: int = 3,
    ) -> None:
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    INSERT INTO article_ai_results (
                        article_id, notion_page_id, field_name, request_group,
                        status, push_status, max_attempts, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, 'skipped', 'skipped', ?, ?, datetime('now'))
                    ON CONFLICT(article_id, field_name) DO UPDATE SET
                        notion_page_id = excluded.notion_page_id,
                        request_group = excluded.request_group,
                        status = 'skipped',
                        push_status = 'skipped',
                        max_attempts = excluded.max_attempts,
                        last_error = excluded.last_error,
                        push_error = NULL,
                        updated_at = datetime('now')
                    """,
                    (article_id, notion_page_id, field_name, request_group, max_attempts, reason),
                )

    def mark_ai_fields_push_processing(self, article_id: int, field_names: list[str]) -> None:
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    UPDATE article_ai_results
                    SET push_status = 'processing',
                        push_attempt_count = push_attempt_count + 1,
                        push_error = NULL,
                        updated_at = datetime('now')
                    WHERE article_id = ? AND field_name = ?
                    """,
                    (article_id, field_name),
                )

    def mark_ai_fields_pushed(self, article_id: int, field_names: list[str]) -> None:
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    UPDATE article_ai_results
                    SET push_status = 'completed',
                        pushed_at = datetime('now'),
                        push_error = NULL,
                        updated_at = datetime('now')
                    WHERE article_id = ? AND field_name = ?
                    """,
                    (article_id, field_name),
                )

    def mark_ai_fields_push_deferred(self, article_id: int, field_names: list[str]) -> None:
        """Preserve completed values that await one atomic event-page apply."""
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    UPDATE article_ai_results
                    SET push_status = 'deferred', push_error = NULL, updated_at = datetime('now')
                    WHERE article_id = ? AND field_name = ? AND status = 'completed'
                    """,
                    (article_id, field_name),
                )

    def mark_ai_fields_push_failed(self, article_id: int, field_names: list[str], error: str) -> None:
        with self.transaction() as conn:
            for field_name in field_names:
                conn.execute(
                    """
                    UPDATE article_ai_results
                    SET push_status = 'failed',
                        push_error = ?,
                        updated_at = datetime('now')
                    WHERE article_id = ? AND field_name = ?
                    """,
                    (error, article_id, field_name),
                )

    def get_ai_articles_pending(self, limit: int = 10, scan_limit: int = 200, max_attempts: int = 3, required_fields: Optional[List[str]] = None) -> List[Article]:
        selected: List[Article] = []
        for article in self.list_recent_synced_articles(limit=scan_limit):
            rows = self.get_ai_results_for_article(article.id)
            actionable = False
            actionable_fields = required_fields or [name for name in AI_ALL_FIELDS if rows.get(name) is not None]
            if not actionable_fields:
                continue
            for field_name in actionable_fields:
                row = rows.get(field_name)
                if row is None:
                    actionable = True
                    break
                if row["status"] not in {"completed", "skipped"} and row["attempt_count"] < max_attempts:
                    actionable = True
                    break
                if row["status"] == "completed" and row["push_status"] != "completed" and row["push_attempt_count"] < max_attempts:
                    actionable = True
                    break
            if actionable:
                selected.append(article)
                if len(selected) >= limit:
                    break
        return selected

    def close(self) -> None:
        """Close database connection."""
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")
