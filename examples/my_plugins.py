from __future__ import annotations

from collections.abc import Iterable, Sequence

from information_filter.interfaces import Sink, Source
from information_filter.models import InformationItem


class DemoSource(Source):
    """Example custom source loaded with type = "my_plugins:DemoSource"."""

    def __init__(self, message: str = "hello") -> None:
        self.message = message

    def collect(self) -> Iterable[InformationItem]:
        yield InformationItem(id="custom-1", title=self.message, source="custom")


class DemoSink(Sink):
    """Example custom output loaded with type = "my_plugins:DemoSink"."""

    def write(self, items: Sequence[InformationItem]) -> int:
        return len(items)
