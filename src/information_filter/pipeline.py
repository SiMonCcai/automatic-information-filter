"""Pipeline orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .interfaces import Processor, Sink, Source, StateStore
from .models import InformationItem
from .state import MemoryStateStore


@dataclass(slots=True)
class RunStats:
    collected: int = 0
    duplicates: int = 0
    accepted: int = 0
    rejected: int = 0
    published: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Pipeline:
    """Collect items, run processors in order, then publish accepted items."""

    def __init__(
        self,
        sources: list[Source],
        processors: list[Processor],
        sinks: list[Sink],
        state: StateStore | None = None,
    ) -> None:
        if not sources:
            raise ValueError("At least one source is required")
        if not sinks:
            raise ValueError("At least one sink is required")
        self.sources = sources
        self.processors = processors
        self.sinks = sinks
        self.state = state or MemoryStateStore()

    def run(self) -> RunStats:
        stats = RunStats()
        accepted: list[InformationItem] = []
        accepted_input_fingerprints: list[str] = []
        run_fingerprints: set[str] = set()

        for source in self.sources:
            for item in source.collect():
                stats.collected += 1
                fingerprint = item.fingerprint
                if fingerprint in run_fingerprints or self.state.contains(fingerprint):
                    stats.duplicates += 1
                    continue
                run_fingerprints.add(fingerprint)

                current: InformationItem | None = item
                for processor in self.processors:
                    if current is None:
                        break
                    current = processor.process(current)

                if current is None:
                    stats.rejected += 1
                    continue

                accepted.append(current)
                accepted_input_fingerprints.append(fingerprint)
                stats.accepted += 1

        for sink in self.sinks:
            sink_name = f"{type(sink).__module__}.{type(sink).__name__}"
            stats.published[sink_name] = sink.write(accepted) if accepted else 0

        self.state.add_many(accepted_input_fingerprints)
        return stats
