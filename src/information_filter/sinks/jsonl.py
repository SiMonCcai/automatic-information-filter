"""Write accepted items as JSON Lines."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ..interfaces import Sink
from ..models import InformationItem


class JSONLinesSink(Sink):
    def __init__(self, path: str, append: bool = True) -> None:
        self.path = Path(path)
        self.append = append

    def write(self, items: Sequence[InformationItem]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self.append else "w"
        with self.path.open(mode, encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        return len(items)
