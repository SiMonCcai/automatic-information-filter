"""Deterministic, local filtering processors."""

from __future__ import annotations

import re

from ..interfaces import Processor
from ..models import InformationItem


def _text(item: InformationItem, fields: list[str]) -> str:
    values: list[str] = []
    for field in fields:
        value = getattr(item, field, item.metadata.get(field, ""))
        values.append(str(value or ""))
    return "\n".join(values)


class KeywordFilter(Processor):
    def __init__(
        self,
        include_any: list[str] | None = None,
        exclude_any: list[str] | None = None,
        fields: list[str] | None = None,
        case_sensitive: bool = False,
    ) -> None:
        self.include_any = include_any or []
        self.exclude_any = exclude_any or []
        self.fields = fields or ["title", "content"]
        self.case_sensitive = case_sensitive

    def process(self, item: InformationItem) -> InformationItem | None:
        haystack = _text(item, self.fields)
        includes = self.include_any
        excludes = self.exclude_any
        if not self.case_sensitive:
            haystack = haystack.casefold()
            includes = [value.casefold() for value in includes]
            excludes = [value.casefold() for value in excludes]
        if includes and not any(value in haystack for value in includes):
            return None
        if excludes and any(value in haystack for value in excludes):
            return None
        return item


class RegexFilter(Processor):
    def __init__(
        self,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        fields: list[str] | None = None,
        ignore_case: bool = True,
    ) -> None:
        flags = re.IGNORECASE if ignore_case else 0
        self.include = [re.compile(value, flags) for value in include or []]
        self.exclude = [re.compile(value, flags) for value in exclude or []]
        self.fields = fields or ["title", "content"]

    def process(self, item: InformationItem) -> InformationItem | None:
        haystack = _text(item, self.fields)
        if self.include and not any(pattern.search(haystack) for pattern in self.include):
            return None
        if self.exclude and any(pattern.search(haystack) for pattern in self.exclude):
            return None
        return item


class MinimumLengthFilter(Processor):
    def __init__(self, minimum: int = 1, field: str = "content") -> None:
        if minimum < 0:
            raise ValueError("minimum must be non-negative")
        self.minimum = minimum
        self.field = field

    def process(self, item: InformationItem) -> InformationItem | None:
        value = getattr(item, self.field, item.metadata.get(self.field, ""))
        return item if len(str(value or "").strip()) >= self.minimum else None
