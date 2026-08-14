"""Read information items from a JSON array or JSON Lines file."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from ..interfaces import Source
from ..models import InformationItem


class JSONFileSource(Source):
    def __init__(self, path: str, source: str = "json") -> None:
        self.path = Path(path)
        self.source = source

    def collect(self) -> Iterable[InformationItem]:
        text = self.path.read_text(encoding="utf-8")
        if self.path.suffix.lower() == ".jsonl":
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            records = json.loads(text)
        if not isinstance(records, list):
            raise ValueError(f"{self.path} must contain a JSON array or JSON Lines records")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Every input record must be a JSON object")
            yield InformationItem.from_mapping(record, source=self.source)
