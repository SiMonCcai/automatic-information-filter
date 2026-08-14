from __future__ import annotations

from collections.abc import Iterable, Sequence

import pytest

from information_filter.interfaces import Processor, Sink, Source
from information_filter.models import InformationItem
from information_filter.pipeline import Pipeline
from information_filter.processors.rules import KeywordFilter
from information_filter.state import MemoryStateStore


class Items(Source):
    def __init__(self, items: list[InformationItem]) -> None:
        self.items = items

    def collect(self) -> Iterable[InformationItem]:
        return iter(self.items)


class Capture(Sink):
    def __init__(self) -> None:
        self.items: list[InformationItem] = []

    def write(self, items: Sequence[InformationItem]) -> int:
        self.items.extend(items)
        return len(items)


class BrokenSink(Sink):
    def write(self, items: Sequence[InformationItem]) -> int:
        raise RuntimeError("sink unavailable")


class ReplaceIdentity(Processor):
    def process(self, item: InformationItem) -> InformationItem:
        return InformationItem(
            id=f"processed-{item.id}",
            title=item.title,
            source="processed",
        )


def test_pipeline_filters_and_deduplicates() -> None:
    kept = InformationItem(id="1", title="Useful automation", source="demo")
    rejected = InformationItem(id="2", title="Advertisement", source="demo")
    sink = Capture()
    stats = Pipeline(
        sources=[Items([kept, kept, rejected])],
        processors=[KeywordFilter(include_any=["automation"], exclude_any=["advertisement"])],
        sinks=[sink],
    ).run()

    assert stats.to_dict() | {"published": {}} == {
        "collected": 3,
        "duplicates": 1,
        "accepted": 1,
        "rejected": 1,
        "published": {},
    }
    assert sink.items == [kept]


def test_state_is_updated_only_after_all_sinks_succeed() -> None:
    item = InformationItem(id="1", source="demo")
    state = MemoryStateStore()
    with pytest.raises(RuntimeError, match="sink unavailable"):
        Pipeline([Items([item])], [], [Capture(), BrokenSink()], state).run()
    assert not state.contains(item.fingerprint)


def test_persistent_dedup_uses_input_identity_before_processing() -> None:
    item = InformationItem(id="1", title="Original", source="input")
    source = Items([item])
    sink = Capture()
    state = MemoryStateStore()

    first = Pipeline([source], [ReplaceIdentity()], [sink], state).run()
    second = Pipeline([source], [ReplaceIdentity()], [sink], state).run()

    assert first.accepted == 1
    assert second.duplicates == 1
    assert len(sink.items) == 1
    assert state.contains(item.fingerprint)
    assert not state.contains(sink.items[0].fingerprint)
