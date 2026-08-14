"""Built-in deduplication state stores."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .interfaces import StateStore


class MemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def contains(self, fingerprint: str) -> bool:
        return fingerprint in self._seen

    def add_many(self, fingerprints: Sequence[str]) -> None:
        self._seen.update(fingerprints)


class SQLiteStateStore(StateStore):
    """Small persistent state store suitable for cron-driven pipelines."""

    def __init__(self, path: str = ".aif/state.db") -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_items (
                fingerprint TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def contains(self, fingerprint: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM processed_items WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def add_many(self, fingerprints: Sequence[str]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO processed_items(fingerprint) VALUES (?)",
            ((value,) for value in fingerprints),
        )
        self.connection.commit()
