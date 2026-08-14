"""TOML configuration and plugin loading."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from .interfaces import Processor, Sink, Source, StateStore
from .pipeline import Pipeline
from .processors import HTTPDecisionProcessor, KeywordFilter, MinimumLengthFilter, RegexFilter
from .sinks import HTTPSink, JSONLinesSink, SQLiteSink, StdoutSink
from .sources import HTTPJSONSource, JSONFileSource, RSSSource
from .state import MemoryStateStore, SQLiteStateStore

BUILTINS: dict[str, dict[str, type]] = {
    "sources": {
        "http_json": HTTPJSONSource,
        "json_file": JSONFileSource,
        "rss": RSSSource,
    },
    "processors": {
        "http_decision": HTTPDecisionProcessor,
        "keyword": KeywordFilter,
        "minimum_length": MinimumLengthFilter,
        "regex": RegexFilter,
    },
    "sinks": {
        "http": HTTPSink,
        "jsonl": JSONLinesSink,
        "sqlite": SQLiteSink,
        "stdout": StdoutSink,
    },
    "state": {
        "memory": MemoryStateStore,
        "sqlite": SQLiteStateStore,
    },
}

EXPECTED = {
    "sources": Source,
    "processors": Processor,
    "sinks": Sink,
    "state": StateStore,
}


def _load_class(category: str, name: str) -> type:
    if name in BUILTINS[category]:
        return BUILTINS[category][name]
    if ":" not in name:
        choices = ", ".join(sorted(BUILTINS[category]))
        raise ValueError(f"Unknown {category} plugin '{name}'. Built-ins: {choices}")
    module_name, class_name = name.split(":", 1)
    module = importlib.import_module(module_name)
    plugin = getattr(module, class_name)
    if not inspect.isclass(plugin):
        raise TypeError(f"Plugin is not a class: {name}")
    return plugin


def _resolve_paths(options: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    result = dict(options)
    path = result.get("path")
    if isinstance(path, str) and path != ":memory:" and not Path(path).is_absolute():
        result["path"] = str((base_dir / path).resolve())
    return result


def _build(category: str, config: dict[str, Any], base_dir: Path) -> Any:
    options = _resolve_paths(config, base_dir)
    plugin_name = options.pop("type", None)
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ValueError(f"Every {category} entry needs a non-empty 'type'")
    plugin_class = _load_class(category, plugin_name)
    instance = plugin_class(**options)
    expected = EXPECTED[category]
    if not isinstance(instance, expected):
        raise TypeError(f"{plugin_name} must implement {expected.__name__}")
    return instance


def load_pipeline(path: str | Path) -> Pipeline:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    base_dir = config_path.parent

    source_configs = config.get("sources", [])
    processor_configs = config.get("processors", [])
    sink_configs = config.get("sinks", [])
    state_config = config.get("state", {"type": "memory"})
    component_configs = (source_configs, processor_configs, sink_configs)
    if not all(isinstance(value, list) for value in component_configs):
        raise ValueError("sources, processors, and sinks must be TOML arrays of tables")
    if not isinstance(state_config, dict):
        raise ValueError("state must be a TOML table")

    return Pipeline(
        sources=[_build("sources", value, base_dir) for value in source_configs],
        processors=[_build("processors", value, base_dir) for value in processor_configs],
        sinks=[_build("sinks", value, base_dir) for value in sink_configs],
        state=_build("state", state_config, base_dir),
    )
