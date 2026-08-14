"""Store accepted items in a portable SQLite table."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from ..interfaces import Sink
from ..models import InformationItem


class SQLiteSink(Sink):
    def __init__(self, path: str = "output.db", table: str = "information_items") -> None:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) is None:
            raise ValueError("table must be a valid unquoted SQLite identifier")
        self.path = Path(path)
        self.table = table

    def write(self, items: Sequence[InformationItem]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    fingerprint TEXT PRIMARY KEY,
                    item_json TEXT NOT NULL,
                    written_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            before = connection.total_changes
            connection.executemany(
                f"INSERT OR IGNORE INTO {self.table}(fingerprint, item_json) VALUES (?, ?)",
                (
                    (item.fingerprint, json.dumps(item.to_dict(), ensure_ascii=False))
                    for item in items
                ),
            )
            written = connection.total_changes - before
            connection.commit()
            return written
        finally:
            connection.close()
