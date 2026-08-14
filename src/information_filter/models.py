"""Provider-neutral information model."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class InformationItem:
    """One normalized unit of information moving through the pipeline."""

    id: str
    title: str = ""
    content: str = ""
    url: str = ""
    source: str = ""
    published_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, source: str = "") -> InformationItem:
        known = {
            "id", "title", "content", "url", "source", "published_at", "metadata", "annotations"
        }
        metadata = dict(value.get("metadata") or {})
        metadata.update({key: val for key, val in value.items() if key not in known})
        item_id = str(value.get("id") or value.get("url") or "")
        if not item_id:
            payload = json.dumps(dict(value), sort_keys=True, default=str).encode()
            item_id = hashlib.sha256(payload).hexdigest()
        published_at = value.get("published_at")
        if isinstance(published_at, datetime):
            published_at = published_at.isoformat()
        return cls(
            id=item_id,
            title=str(value.get("title") or ""),
            content=str(value.get("content") or ""),
            url=str(value.get("url") or ""),
            source=str(value.get("source") or source),
            published_at=str(published_at) if published_at else None,
            metadata=metadata,
            annotations=dict(value.get("annotations") or {}),
        )

    @property
    def fingerprint(self) -> str:
        identity = self.url or self.id
        return hashlib.sha256(f"{self.source}\0{identity}".encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
