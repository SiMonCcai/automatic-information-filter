import json
import sqlite3

import pytest

from information_filter.models import InformationItem
from information_filter.sinks.jsonl import JSONLinesSink
from information_filter.sinks.sqlite import SQLiteSink
from information_filter.sources.json_file import JSONFileSource
from information_filter.state import SQLiteStateStore


def test_json_file_to_json_lines(tmp_path) -> None:
    source_path = tmp_path / "items.json"
    output_path = tmp_path / "accepted.jsonl"
    source_path.write_text(json.dumps([{"id": "1", "title": "Hello"}]), encoding="utf-8")
    items = list(JSONFileSource(str(source_path), source="test").collect())
    assert JSONLinesSink(str(output_path), append=False).write(items) == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["source"] == "test"


def test_sqlite_sink_and_state(tmp_path) -> None:
    item = InformationItem(id="1", source="test")
    output = tmp_path / "output.db"
    assert SQLiteSink(str(output)).write([item]) == 1
    with sqlite3.connect(output) as connection:
        assert connection.execute("SELECT count(*) FROM information_items").fetchone()[0] == 1

    state = SQLiteStateStore(str(tmp_path / "state.db"))
    assert not state.contains(item.fingerprint)
    state.add_many([item.fingerprint])
    assert state.contains(item.fingerprint)


def test_sqlite_sink_rejects_invalid_table_names(tmp_path) -> None:
    with pytest.raises(ValueError, match="valid unquoted SQLite identifier"):
        SQLiteSink(str(tmp_path / "items.db"), table="items; DROP TABLE items")
