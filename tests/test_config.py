import json

import pytest

from information_filter.config import load_pipeline


def test_config_resolves_relative_paths_and_runs(tmp_path) -> None:
    (tmp_path / "items.json").write_text(
        json.dumps([{"id": "1", "title": "Keep this", "content": "long enough"}]),
        encoding="utf-8",
    )
    (tmp_path / "pipeline.toml").write_text(
        """
[[sources]]
type = "json_file"
path = "items.json"

[[processors]]
type = "keyword"
include_any = ["keep"]

[[sinks]]
type = "jsonl"
path = "output/items.jsonl"
append = false

[state]
type = "sqlite"
path = "output/state.db"
""",
        encoding="utf-8",
    )
    pipeline_path = tmp_path / "pipeline.toml"
    stats = load_pipeline(pipeline_path).run()
    output_path = tmp_path / "output/items.jsonl"
    original_output = output_path.read_text(encoding="utf-8")
    assert stats.accepted == 1

    second = load_pipeline(pipeline_path).run()
    assert second.duplicates == 1
    assert output_path.read_text(encoding="utf-8") == original_output


def test_config_rejects_unknown_plugin(tmp_path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text(
        '[[sources]]\ntype="missing"\n[[sinks]]\ntype="stdout"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Unknown sources plugin"):
        load_pipeline(config)


def test_config_loads_custom_plugins(tmp_path, monkeypatch) -> None:
    (tmp_path / "custom_plugin.py").write_text(
        """
from information_filter.interfaces import Source
from information_filter.models import InformationItem

class CustomSource(Source):
    def __init__(self, title):
        self.title = title
    def collect(self):
        yield InformationItem(id="custom-1", title=self.title, source="custom")
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(tmp_path)
    config = tmp_path / "custom.toml"
    config.write_text(
        """
[[sources]]
type = "custom_plugin:CustomSource"
title = "Loaded"

[[sinks]]
type = "jsonl"
path = "custom-output.jsonl"
append = false
""",
        encoding="utf-8",
    )
    stats = load_pipeline(config).run()
    assert stats.accepted == 1
    assert "Loaded" in (tmp_path / "custom-output.jsonl").read_text(encoding="utf-8")
