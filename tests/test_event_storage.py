import sqlite3
from datetime import datetime, timedelta, timezone

from pipeline.storage import Storage


MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_feature_off_unsynced_query_restores_legacy_visibility_for_event_members(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    feed = storage.add_feed("feed", "https://feed.test")
    first = storage.add_article(feed.id, "first", "https://first", None, "body", "body", None, None)
    second = storage.add_article(feed.id, "second", "https://second", None, "body", "body", None, None)
    event_id = storage.create_event(MODEL, event_id="rollback")
    for article in (first, second):
        storage.append_event_member(
            event_id=event_id, article_id=article.id, title=article.title,
            url=article.url, source="feed", embedding=b"vector",
        )

    assert [row.id for row in storage.get_unsynced_articles()] == [first.id]
    assert [row.id for row in storage.get_unsynced_articles(event_mode=False)] == [first.id, second.id]


def table_columns(storage: Storage, table: str) -> set[str]:
    return {
        row[1]
        for row in storage._get_conn().execute(f"PRAGMA table_info({table})").fetchall()
    }


def test_create_event_and_append_member_are_persistent_and_idempotent(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    event_id = storage.create_event(MODEL, event_id="evt-stable")
    embedding = b"\x00\x00\x80?\x00\x00\x00\x00"

    inserted = storage.append_event_member(
        event_id=event_id,
        article_id=42,
        title="A title",
        url="https://example.com/a",
        source="Feed A",
        embedding=embedding,
        similarity=0.98,
    )
    duplicate = storage.append_event_member(
        event_id=event_id,
        article_id=42,
        title="changed title",
        url="https://example.com/changed",
        source="Feed B",
        embedding=embedding,
        similarity=0.99,
    )

    event = storage._get_conn().execute(
        "SELECT * FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    member = storage._get_conn().execute(
        "SELECT * FROM event_members WHERE article_id = ?", (42,)
    ).fetchone()
    assert inserted is True
    assert duplicate is False
    assert event["member_count"] == 1
    assert event["embedding_model"] == MODEL
    assert member["event_id"] == event_id
    assert member["title"] == "A title"
    assert member["embedding"] == embedding


def test_event_updates_page_scores_and_winner_replacement_atomically(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    event_id = storage.create_event(MODEL)
    for article_id in (10, 11):
        storage.append_event_member(
            event_id=event_id,
            article_id=article_id,
            title=f"title-{article_id}",
            url=f"https://example.com/{article_id}",
            source="feed",
            embedding=b"\x00\x00\x80?",
        )

    storage.set_event_page_id(event_id, "page-stable")
    storage.set_event_member_scores(11, score_total=23.5, score_count=6)
    storage.set_event_winner(event_id, 10, score_total=20.0, score_count=6)
    storage.set_event_winner(
        event_id, 11, score_total=23.5, score_count=6, replacement=True
    )

    event = dict(
        storage._get_conn().execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    )
    member = dict(
        storage._get_conn().execute(
            "SELECT * FROM event_members WHERE article_id = ?", (11,)
        ).fetchone()
    )
    assert event["notion_page_id"] == "page-stable"
    assert event["current_winner_article_id"] == 11
    assert event["current_winner_score_total"] == 23.5
    assert event["current_winner_score_count"] == 6
    assert event["replacement_count"] == 1
    assert member["score_total"] == 23.5
    assert member["score_count"] == 6


def test_recent_members_filters_by_seven_day_window_and_model(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    recent_event = storage.create_event(MODEL, event_id="recent")
    old_event = storage.create_event(MODEL, event_id="old")
    other_event = storage.create_event("other-model", event_id="other")
    for event_id, article_id in ((recent_event, 1), (old_event, 2), (other_event, 3)):
        storage.append_event_member(
            event_id=event_id,
            article_id=article_id,
            title=f"title-{article_id}",
            url=f"https://example.com/{article_id}",
            source="feed",
            embedding=b"\x00\x00\x80?",
        )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE event_members SET created_at = datetime('now', '-8 days') WHERE article_id = ?",
            (2,),
        )

    members = storage.get_recent_event_members(MODEL)

    assert [row["article_id"] for row in members] == [1]
    assert members[0]["event_id"] == "recent"
    assert members[0]["embedding"] == b"\x00\x00\x80?"


def test_event_schema_contains_structured_event_and_member_fields(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))

    assert table_columns(storage, "events") == {
        "id",
        "notion_page_id",
        "current_winner_article_id",
        "current_winner_score_total",
        "current_winner_score_count",
        "member_count",
        "first_seen_at",
        "last_seen_at",
        "replacement_count",
        "embedding_model",
        "state",
        "revision",
        "presented_revision",
        "pending_reset_revision",
        "reset_done_revision",
        "winner_operation_id",
    }
    assert table_columns(storage, "event_members") == {
        "event_id",
        "article_id",
        "title",
        "url",
        "source",
        "embedding",
        "similarity",
        "score_total",
        "score_count",
        "candidate_status",
        "reading_reset_done",
        "member_revision",
        "notion_page_id",
        "synced_at",
        "created_at",
        "updated_at",
    }
    assert table_columns(storage, "event_replacements") == {
        "id", "event_id", "old_article_id", "new_article_id",
        "old_score_total", "new_score_total", "operation_id", "created_at",
    }


def test_event_winner_transition_is_compare_and_swap_and_operation_idempotent(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    event_id = storage.create_event(MODEL, event_id="evt-cas")
    for article_id in (1, 2):
        storage.append_event_member(
            event_id=event_id,
            article_id=article_id,
            title=str(article_id),
            url=f"https://example.test/{article_id}",
            source="feed",
            embedding=b"",
        )

    assert storage.set_event_winner(
        event_id, 1, score_total=20, score_count=6,
        expected_old_winner=None, operation_id="initial-1",
    ) is True
    assert storage.set_event_winner(
        event_id, 2, score_total=22, score_count=6, replacement=True,
        expected_old_winner=1, operation_id="replace-2",
    ) is True
    assert storage.set_event_winner(
        event_id, 2, score_total=22, score_count=6, replacement=True,
        expected_old_winner=1, operation_id="replace-2",
    ) is True
    assert storage.set_event_winner(
        event_id, 1, score_total=25, score_count=6, replacement=True,
        expected_old_winner=1, operation_id="stale",
    ) is False

    event = storage.get_event(event_id)
    assert event["current_winner_article_id"] == 2
    assert event["replacement_count"] == 1
    assert event["winner_operation_id"] == "replace-2"
    history = storage.list_event_replacements(event_id)
    assert [(row["old_article_id"], row["new_article_id"], row["operation_id"]) for row in history] == [
        (1, 2, "replace-2")
    ]


def test_cleanup_keeps_event_member_links_and_counts_after_raw_article_expiry(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    feed = storage.add_feed("feed", "https://feed.test")
    article = storage.add_article(
        feed.id, "Archived member", "https://article.test", "author",
        "body", "body", None, None,
    )
    event_id = storage.create_event(MODEL)
    storage.append_event_member(
        event_id=event_id,
        article_id=article.id,
        title=article.title,
        url=article.url,
        source="feed",
        embedding=b"",
    )
    storage.mark_event_canonical_synced(event_id, "page")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE articles_raw SET synced_at=datetime('now','-31 days'), fetched_at=datetime('now','-31 days') WHERE id=?",
            (article.id,),
        )

    assert storage.cleanup_synced_articles(keep_days=30) == 1
    assert storage.get_article(article.id) is None
    members = storage.list_event_members(event_id)
    assert [(row["title"], row["url"]) for row in members] == [
        ("Archived member", "https://article.test")
    ]
    assert storage.get_event(event_id)["member_count"] == 1
