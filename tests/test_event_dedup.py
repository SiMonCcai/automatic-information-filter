import logging

import pytest

from pipeline.event_dedup import (
    EMBEDDING_MODEL,
    EventCandidate,
    SubprocessTitleEmbedder,
    TitleEventMatcher,
    blob_to_vector,
    vector_to_blob,
)
from pipeline.storage import Storage


def test_invalid_titles_fail_open_without_preventing_valid_truncated_titles(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    existing = storage.create_event(EMBEDDING_MODEL, event_id="existing")
    storage.append_event_member(
        event_id=existing,
        article_id=1,
        title="existing",
        url="https://example.com/1",
        source="feed",
        embedding=vector_to_blob([1.0, 0.0]),
    )
    calls = []

    def embedder(titles):
        calls.append(titles)
        return [[1.0, 0.0]]

    results = TitleEventMatcher(storage, embedder=embedder).match_and_store(
        [
            EventCandidate(2, "", "https://example.com/2"),
            EventCandidate(3, "x" * 600, "https://example.com/3"),
        ]
    )

    assert results[0].is_new_event is True
    assert results[1].event_id == existing
    assert calls == [["x" * 512]]


def test_embedding_failure_logs_and_fails_open_to_separate_new_events(tmp_path, caplog):
    storage = Storage(str(tmp_path / "pipeline.db"))

    def broken_embedder(titles):
        raise RuntimeError("helper crashed")

    matcher = TitleEventMatcher(storage, embedder=broken_embedder)
    with caplog.at_level(logging.ERROR):
        results = matcher.match_and_store(
            [
                EventCandidate(10, "one", "https://example.com/10"),
                EventCandidate(11, "two", "https://example.com/11"),
            ]
        )

    assert len({result.event_id for result in results}) == 2
    assert all(result.is_new_event for result in results)
    assert "helper crashed" in caplog.text
    blobs = storage._get_conn().execute(
        "SELECT embedding FROM event_members ORDER BY article_id"
    ).fetchall()
    assert [row[0] for row in blobs] == [b"", b""]


def test_failed_embedding_members_are_ignored_by_later_matching(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    failed_event = storage.create_event(EMBEDDING_MODEL, event_id="failed")
    storage.append_event_member(
        event_id=failed_event,
        article_id=1,
        title="failed",
        url="https://example.com/failed",
        source="feed",
        embedding=b"",
    )

    result = TitleEventMatcher(
        storage,
        embedder=lambda titles: [[1.0, 0.0]],
    ).match_and_store([EventCandidate(2, "valid", "https://example.com/valid")])

    assert result[0].is_new_event is True
    assert result[0].event_id != failed_event


def test_matcher_chooses_highest_recent_cosine_and_matches_inside_batch(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    existing = storage.create_event(EMBEDDING_MODEL, event_id="existing")
    storage.append_event_member(
        event_id=existing,
        article_id=1,
        title="existing",
        url="https://example.com/1",
        source="feed",
        embedding=vector_to_blob([1.0, 0.0]),
    )
    calls = []

    def embedder(titles):
        calls.append(titles)
        return [[0.9999, 0.01], [0.0, 1.0], [0.0, 0.9999]]

    matcher = TitleEventMatcher(storage, embedder=embedder)
    results = matcher.match_and_store(
        [
            EventCandidate(2, "same story", "https://example.com/2", "feed-2"),
            EventCandidate(3, "new story", "https://example.com/3", "feed-3"),
            EventCandidate(4, "new story update", "https://example.com/4", "feed-4"),
        ]
    )

    assert calls == [["same story", "new story", "new story update"]]
    assert results[0].event_id == existing
    assert results[0].is_new_event is False
    assert results[0].similarity >= 0.96
    assert results[1].is_new_event is True
    assert results[2].event_id == results[1].event_id
    assert results[2].is_new_event is False
    assert blob_to_vector(
        storage._get_conn().execute(
            "SELECT embedding FROM event_members WHERE article_id = 2"
        ).fetchone()[0]
    ) == pytest.approx([0.9999, 0.01], rel=1e-6)


def test_matcher_chooses_highest_similarity_across_multiple_events(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    for event_id, article_id, vector in (
        ("weak", 1, [0.8, 0.6]),
        ("best", 2, [1.0, 0.0]),
    ):
        storage.create_event(EMBEDDING_MODEL, event_id=event_id)
        storage.append_event_member(
            event_id=event_id,
            article_id=article_id,
            title=event_id,
            url=f"https://{event_id}.test",
            source="feed",
            embedding=vector_to_blob(vector),
        )

    result = TitleEventMatcher(
        storage,
        embedder=lambda titles: [[0.99, 0.01]],
        threshold=0.7,
    ).match_and_store([EventCandidate(3, "candidate", "https://candidate.test")])

    assert result[0].event_id == "best"


def test_dimension_mismatch_is_ignored_per_existing_vector(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    storage.create_event(EMBEDDING_MODEL, event_id="old-dimension")
    storage.append_event_member(
        event_id="old-dimension",
        article_id=1,
        title="old",
        url="https://old.test",
        source="feed",
        embedding=vector_to_blob([1.0, 0.0]),
    )

    result = TitleEventMatcher(
        storage,
        embedder=lambda titles: [[1.0, 0.0, 0.0]],
    ).match_and_store([EventCandidate(2, "new", "https://new.test")])

    assert result[0].is_new_event is True
    assert storage.get_event(result[0].event_id)["member_count"] == 1


def test_non_finite_vector_fails_open_for_only_that_article(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    matcher = TitleEventMatcher(
        storage,
        embedder=lambda titles: [[float("nan"), 0.0], [1.0, 0.0]],
    )

    result = matcher.match_and_store(
        [
            EventCandidate(1, "bad", "https://bad.test"),
            EventCandidate(2, "good", "https://good.test"),
        ]
    )

    assert len(result) == 2
    blobs = storage._get_conn().execute(
        "SELECT article_id, embedding FROM event_members ORDER BY article_id"
    ).fetchall()
    assert blobs[0]["embedding"] == b""
    assert blobs[1]["embedding"] != b""


def test_wrong_model_output_dimension_fails_open_as_independent_event(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    matcher = TitleEventMatcher(
        storage,
        embedder=lambda titles: [[1.0, 0.0, 0.0] for _ in titles],
        expected_dimension=2,
    )

    match = matcher.match_and_store([
        EventCandidate(91, "wrong dimension", "https://wrong.test", "feed")
    ])[0]

    assert match.is_new_event is True
    assert storage.list_event_members(match.event_id)[0]["embedding"] == b""


def test_matcher_retry_returns_existing_membership_without_orphan_event(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    matcher = TitleEventMatcher(storage, embedder=lambda titles: [[1.0, 0.0]])
    candidate = EventCandidate(1, "same", "https://same.test")

    first = matcher.match_and_store([candidate])[0]
    retried = TitleEventMatcher(
        storage, embedder=lambda titles: [[0.0, 1.0]]
    ).match_and_store([candidate])[0]

    assert retried.event_id == first.event_id
    assert storage._get_conn().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_subprocess_embedder_has_bounded_timeout(monkeypatch, tmp_path):
    captured = {}

    class Completed:
        stdout = '{"embeddings": [[1.0, 0.0]]}'

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr("pipeline.event_dedup.subprocess.run", fake_run)
    embedder = SubprocessTitleEmbedder(
        python=tmp_path / "python",
        helper_script=tmp_path / "helper.py",
        cache_dir=tmp_path / "cache",
    )

    assert embedder(["title"]) == [[1.0, 0.0]]
    assert captured["timeout"] == 180
