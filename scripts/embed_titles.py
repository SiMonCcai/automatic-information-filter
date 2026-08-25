#!/usr/bin/env python3
"""Low-memory FastEmbed JSON helper used by the main pipeline subprocess."""

from __future__ import annotations

import json
import sys
from typing import Callable


def embed_titles(
    titles: list[str],
    *,
    model_factory: Callable,
    model_name: str,
    cache_dir: str,
    threads: int = 1,
    batch_size: int = 8,
) -> list[list[float]]:
    model = model_factory(
        model_name=model_name,
        cache_dir=cache_dir,
        threads=max(1, int(threads)),
    )
    return [
        [float(value) for value in vector]
        for vector in model.embed(titles, batch_size=max(1, int(batch_size)))
    ]


def main() -> int:
    from fastembed import TextEmbedding

    payload = json.load(sys.stdin)
    titles = payload.get("titles")
    if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
        raise ValueError("titles must be a JSON array of strings")

    vectors = embed_titles(
        titles,
        model_factory=TextEmbedding,
        model_name=str(payload["model_name"]),
        cache_dir=str(payload["cache_dir"]),
        threads=int(payload.get("threads", 1)),
        batch_size=int(payload.get("batch_size", 8)),
    )
    json.dump({"embeddings": vectors}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
