"""Extension interfaces for sources, processors, sinks, and state stores."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence

from .models import InformationItem


class Source(ABC):
    @abstractmethod
    def collect(self) -> Iterable[InformationItem]:
        """Yield normalized items."""


class Processor(ABC):
    @abstractmethod
    def process(self, item: InformationItem) -> InformationItem | None:
        """Return the item to keep it, or None to reject it."""


class Sink(ABC):
    @abstractmethod
    def write(self, items: Sequence[InformationItem]) -> int:
        """Publish items and return the number written."""


class StateStore(ABC):
    @abstractmethod
    def contains(self, fingerprint: str) -> bool:
        """Return whether an item has already completed the pipeline."""

    @abstractmethod
    def add_many(self, fingerprints: Sequence[str]) -> None:
        """Remember successfully published items."""
