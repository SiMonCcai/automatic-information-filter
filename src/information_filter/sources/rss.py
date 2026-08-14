"""RSS and Atom source adapter."""

from __future__ import annotations

from collections.abc import Iterable

import feedparser

from ..interfaces import Source
from ..models import InformationItem


class RSSSource(Source):
    def __init__(self, url: str, source: str = "rss") -> None:
        self.url = url
        self.source = source

    def collect(self) -> Iterable[InformationItem]:
        feed = feedparser.parse(self.url)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError(f"Could not parse feed: {self.url}")
        for entry in feed.entries:
            content_parts = entry.get("content") or []
            content = "\n".join(str(part.get("value", "")) for part in content_parts)
            if not content:
                content = str(entry.get("summary") or entry.get("description") or "")
            url = str(entry.get("link") or "")
            yield InformationItem.from_mapping(
                {
                    "id": entry.get("id") or url,
                    "title": entry.get("title") or "",
                    "content": content,
                    "url": url,
                    "published_at": entry.get("published") or entry.get("updated"),
                    "metadata": {"author": entry.get("author") or ""},
                },
                source=self.source,
            )
