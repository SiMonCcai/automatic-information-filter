"""Write accepted items to standard output."""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..interfaces import Sink
from ..models import InformationItem


class StdoutSink(Sink):
    def __init__(self, pretty: bool = False) -> None:
        self.pretty = pretty

    def write(self, items: Sequence[InformationItem]) -> int:
        for item in items:
            indent = 2 if self.pretty else None
            print(json.dumps(item.to_dict(), ensure_ascii=False, indent=indent))
        return len(items)
