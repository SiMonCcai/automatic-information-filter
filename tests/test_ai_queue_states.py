from pathlib import Path

from pipeline.storage import Storage
from tests.test_ai_queue import add_synced_article


def seed_result(storage: Storage, article_id: int, field: str, status: str, push_status: str):
    with storage.transaction() as conn:
        conn.execute(
            """
            INSERT INTO article_ai_results (
                article_id, notion_page_id, field_name, request_group, status, push_status
            ) VALUES (?, 'page', ?, 'score', ?, ?)
            """,
            (article_id, field, status, push_status),
        )


def test_ai_queue_terminal_states_are_computed_per_article(tmp_path: Path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    complete = add_synced_article(storage, "complete")
    failed = add_synced_article(storage, "failed")
    pending = add_synced_article(storage, "pending")
    for article_id in (complete, failed, pending):
        storage.enqueue_ai_articles([article_id])

    seed_result(storage, complete, "摘要", "completed", "completed")
    seed_result(storage, complete, "实用性", "skipped", "skipped")
    seed_result(storage, failed, "摘要", "failed", "pending")
    seed_result(storage, failed, "实用性", "completed", "completed")
    seed_result(storage, pending, "摘要", "completed", "completed")

    states = storage.get_ai_enrichment_states(
        [complete, failed, pending], ["摘要", "实用性"]
    )
    assert states == {complete: "completed", failed: "failed", pending: "pending"}
