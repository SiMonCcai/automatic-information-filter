from pathlib import Path

from pipeline.storage import Storage


def claim(storage: Storage, limit: int = 10, stale_after_minutes: int = 30):
    result = storage.claim_ai_queue(limit, stale_after_minutes)
    return result.token, result.article_ids


def add_synced_article(storage: Storage, title: str) -> int:
    with storage.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO feeds (name, url) VALUES ('feed', 'https://feed.example/rss')"
        )
        feed_id = conn.execute(
            "SELECT id FROM feeds WHERE url = 'https://feed.example/rss'"
        ).fetchone()[0]
        cursor = conn.execute(
            """
            INSERT INTO articles_raw (
                feed_id, title, url, content_text, synced_at, notion_page_id
            ) VALUES (?, ?, ?, 'substantive article body', datetime('now'), ?)
            """,
            (feed_id, title, f"https://example.com/{title}", f"page-{title}"),
        )
        return cursor.lastrowid


def test_storage_startup_backfills_synced_articles_that_never_received_ai_results(tmp_path: Path):
    db = tmp_path / "pipeline.db"
    storage = Storage(str(db))
    article_id = add_synced_article(storage, "legacy-unprocessed")
    storage._get_conn().close()

    reopened = Storage(str(db))

    assert claim(reopened)[1] == [article_id]


def test_queue_is_persistent_fifo_and_enqueue_is_idempotent(tmp_path: Path):
    db = tmp_path / "pipeline.db"
    storage = Storage(str(db))
    first = add_synced_article(storage, "first")
    second = add_synced_article(storage, "second")

    assert storage.enqueue_ai_articles([first, second, first]) == 2
    assert claim(storage, 1)[1] == [first]

    reopened = Storage(str(db))
    assert claim(reopened)[1] == [second]


def test_queue_entries_can_complete_or_return_to_pending(tmp_path: Path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    first = add_synced_article(storage, "first")
    second = add_synced_article(storage, "second")
    storage.enqueue_ai_articles([first, second])
    token, ids = claim(storage, 2)
    assert ids == [first, second]

    storage.complete_ai_queue([first], token)
    storage.release_ai_queue([second], token, "worker interrupted")

    assert claim(storage)[1] == []
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE ai_enrichment_queue SET available_at = datetime('now', '-1 minute') WHERE article_id = ?",
            (second,),
        )
    assert claim(storage)[1] == [second]
    rows = storage.get_ai_queue_rows([first, second])
    assert rows[first]["status"] == "completed"
    assert rows[second]["status"] == "processing"
    assert rows[second]["attempt_count"] == 2


def test_repeated_worker_releases_eventually_fail_instead_of_starving_fifo(tmp_path: Path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article_id = add_synced_article(storage, "poison")
    storage.enqueue_ai_articles([article_id])
    for attempt in range(5):
        token, ids = claim(storage, 1)
        assert ids == [article_id]
        storage.release_ai_queue([article_id], token, "transient worker failure")
        if attempt < 4:
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE ai_enrichment_queue SET available_at = datetime('now', '-1 minute') WHERE article_id = ?",
                    (article_id,),
                )

    row = storage.get_ai_queue_rows([article_id])[article_id]
    assert row["status"] == "failed"
    assert claim(storage, 1)[1] == []


def test_cleanup_preserves_old_articles_with_active_ai_queue_work(tmp_path: Path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    pending = add_synced_article(storage, "old-pending")
    completed = add_synced_article(storage, "old-completed")
    storage.enqueue_ai_articles([pending, completed])
    token, ids = claim(storage, 1)
    assert ids == [pending]
    storage.complete_ai_queue([pending], token)
    # Requeue the first article as active and leave the second queue row completed.
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE ai_enrichment_queue SET status='pending', completed_at=NULL WHERE article_id=?",
            (pending,),
        )
        conn.execute(
            "UPDATE ai_enrichment_queue SET status='completed', completed_at=datetime('now') WHERE article_id=?",
            (completed,),
        )
        conn.execute(
            "UPDATE articles_raw SET fetched_at=datetime('now', '-40 days') WHERE id IN (?, ?)",
            (pending, completed),
        )

    assert storage.cleanup_synced_articles(keep_days=30) == 1
    assert storage.get_article(pending) is not None
    assert storage.get_article(completed) is None


def test_stale_processing_claims_are_recovered(tmp_path: Path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article_id = add_synced_article(storage, "stale")
    storage.enqueue_ai_articles([article_id])
    first_token, ids = claim(storage, 1)
    assert ids == [article_id]
    assert storage.renew_ai_queue_claim([article_id], "wrong-token") == 0
    assert storage.renew_ai_queue_claim([article_id], first_token) == 1
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE ai_enrichment_queue SET claimed_at = datetime('now', '-2 hours') WHERE article_id = ?",
            (article_id,),
        )

    assert claim(storage, 1, stale_after_minutes=30)[1] == []
    row = storage.get_ai_queue_rows([article_id])[article_id]
    assert row["status"] == "pending"
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE ai_enrichment_queue SET available_at = datetime('now', '-1 minute') WHERE article_id = ?",
            (article_id,),
        )
    second_token, ids = claim(storage, 1, stale_after_minutes=30)
    assert ids == [article_id]
    storage.complete_ai_queue([article_id], first_token)
    assert storage.get_ai_queue_rows([article_id])[article_id]["status"] == "processing"
    storage.complete_ai_queue([article_id], second_token)
    assert storage.get_ai_queue_rows([article_id])[article_id]["status"] == "completed"
