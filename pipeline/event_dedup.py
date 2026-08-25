"""Title-embedding event matching without a vector database."""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from pipeline.storage import Storage

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SIMILARITY_THRESHOLD = 0.96
EMBEDDING_BATCH_SIZE = 8
EMBEDDING_THREADS = 1
MODEL_INPUT_MAX_CHARS = 512
MAX_TITLE_CHARS = 10_000
PRODUCTION_PYTHON = Path("/root/rss-pipeline/.venv-embedding/bin/python")
PRODUCTION_CACHE = Path("/root/rss-pipeline/.embedding-cache")
HELPER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "embed_titles.py"


@dataclass(frozen=True)
class EventCandidate:
    article_id: int
    title: object
    url: str
    source: Optional[str] = None


@dataclass(frozen=True)
class EventMatch:
    article_id: int
    event_id: str
    is_new_event: bool
    similarity: Optional[float]


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """Encode an embedding as little-endian float32 values."""
    values = [float(value) for value in vector]
    return struct.pack(f"<{len(values)}f", *values)


def blob_to_vector(blob: bytes) -> list[float]:
    """Decode a float32 embedding BLOB."""
    if len(blob) % 4:
        raise ValueError("embedding BLOB length is not divisible by four")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


class TitleEventMatcher:
    def __init__(
        self,
        storage: Storage,
        *,
        embedder: Optional[Callable[[list[str]], Sequence[Sequence[float]]]] = None,
        threshold: float = SIMILARITY_THRESHOLD,
        model_name: str = EMBEDDING_MODEL,
        window_days: int = 7,
        expected_dimension: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.storage = storage
        self.embedder = embedder or SubprocessTitleEmbedder()
        self.threshold = threshold
        self.model_name = model_name
        self.window_days = window_days
        self.expected_dimension = expected_dimension
        self.logger = logger or logging.getLogger(__name__)

    def match_and_store(self, candidates: Sequence[EventCandidate]) -> list[EventMatch]:
        """Embed and assign candidates, including matches within this batch."""
        if not candidates:
            return []
        results: list[Optional[EventMatch]] = [None] * len(candidates)
        valid: list[tuple[int, EventCandidate, str]] = []
        for index, candidate in enumerate(candidates):
            existing = self.storage._get_conn().execute(
                "SELECT event_id, similarity FROM event_members WHERE article_id = ?",
                (candidate.article_id,),
            ).fetchone()
            if existing is not None:
                results[index] = EventMatch(
                    candidate.article_id,
                    existing["event_id"],
                    False,
                    existing["similarity"],
                )
                continue
            try:
                valid.append((index, candidate, self._model_title(candidate.title)))
            except (TypeError, ValueError) as exc:
                self.logger.warning("Invalid title for article %s; failing open: %s", candidate.article_id, exc)
                results[index] = self._store_new_without_embedding(candidate)

        if not valid:
            return [result for result in results if result is not None]

        try:
            titles = [title for _, _, title in valid]
            vectors = [list(map(float, row)) for row in self.embedder(titles)]
            if len(vectors) != len(valid):
                raise ValueError("embedding helper returned the wrong number of vectors")
        except Exception as exc:
            self.logger.error("Title embedding failed; creating new events: %s", exc)
            for index, candidate, _ in valid:
                results[index] = self._store_new_without_embedding(candidate)
            return [result for result in results if result is not None]

        pool = []
        for row in self.storage.get_recent_event_members(self.model_name, self.window_days):
            if not row["embedding"]:
                continue
            try:
                vector = blob_to_vector(row["embedding"])
            except (TypeError, ValueError) as exc:
                self.logger.warning("Ignoring corrupt event embedding for article %s: %s", row["article_id"], exc)
                continue
            if vector and (self.expected_dimension is None or len(vector) == self.expected_dimension):
                pool.append((row["event_id"], vector))

        for (index, candidate, _), vector in zip(valid, vectors):
            if (
                not vector
                or (self.expected_dimension is not None and len(vector) != self.expected_dimension)
                or not all(math.isfinite(value) for value in vector)
            ):
                self.logger.error(
                    "Invalid title embedding for article %s; creating a separate event",
                    candidate.article_id,
                )
                results[index] = self._store_new_without_embedding(candidate)
                continue
            event_id, similarity = self._best_match(vector, pool)
            is_new = event_id is None
            if is_new:
                event_id = self.storage.create_event(self.model_name)
                similarity = None
            self.storage.append_event_member(
                event_id=event_id,
                article_id=candidate.article_id,
                title=str(candidate.title),
                url=candidate.url,
                source=candidate.source,
                embedding=vector_to_blob(vector),
                similarity=similarity,
            )
            pool.append((event_id, vector))
            results[index] = EventMatch(candidate.article_id, event_id, is_new, similarity)
        return [result for result in results if result is not None]

    def _store_new_without_embedding(self, candidate: EventCandidate) -> EventMatch:
        event_id = self.storage.create_event(self.model_name)
        self.storage.append_event_member(
            event_id=event_id,
            article_id=candidate.article_id,
            title=str(candidate.title) if candidate.title is not None else "",
            url=candidate.url,
            source=candidate.source,
            embedding=b"",
        )
        return EventMatch(candidate.article_id, event_id, True, None)

    @staticmethod
    def _model_title(title: object) -> str:
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
            raise ValueError("title is empty, malformed, or overlong")
        return title.strip()[:MODEL_INPUT_MAX_CHARS]

    def _best_match(
        self, vector: Sequence[float], pool: Iterable[tuple[str, Sequence[float]]]
    ) -> tuple[Optional[str], Optional[float]]:
        best_event = None
        best_similarity = -1.0
        for event_id, member_vector in pool:
            if len(vector) != len(member_vector):
                continue
            similarity = cosine_similarity(vector, member_vector)
            if similarity > best_similarity:
                best_event, best_similarity = event_id, similarity
        if best_similarity >= self.threshold:
            return best_event, best_similarity
        return None, None


class SubprocessTitleEmbedder:
    """Invoke the isolated production sentence-transformers environment."""

    def __init__(
        self,
        python: Path = PRODUCTION_PYTHON,
        helper_script: Path = HELPER_SCRIPT,
        cache_dir: Path = PRODUCTION_CACHE,
        model_name: str = EMBEDDING_MODEL,
        batch_size: int = EMBEDDING_BATCH_SIZE,
        threads: int = EMBEDDING_THREADS,
        timeout_seconds: int = 180,
    ):
        self.python = Path(python)
        self.helper_script = Path(helper_script)
        self.cache_dir = Path(cache_dir)
        self.model_name = model_name
        self.batch_size = max(1, int(batch_size))
        self.threads = max(1, int(threads))
        self.timeout_seconds = max(1, int(timeout_seconds))

    def __call__(self, titles: list[str]) -> list[list[float]]:
        env = os.environ.copy()
        env["HF_HOME"] = str(self.cache_dir)
        completed = subprocess.run(
            [str(self.python), str(self.helper_script)],
            input=json.dumps(
                {
                    "titles": titles,
                    "model_name": self.model_name,
                    "cache_dir": str(self.cache_dir),
                    "batch_size": self.batch_size,
                    "threads": self.threads,
                }
            ),
            text=True,
            capture_output=True,
            check=True,
            timeout=self.timeout_seconds,
            env=env,
        )
        payload = json.loads(completed.stdout)
        return payload["embeddings"]
