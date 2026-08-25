from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pipeline import runner
from pipeline.event_dedup import vector_to_blob
from pipeline.storage import AIQueueClaim, Storage
from tests.test_ai_queue import add_synced_article


class FakeFetcher:
    def __init__(self, storage, config):
        pass

    def fetch_all(self, enabled_only=True):
        return {
            "articles_added": 0,
            "articles_skipped": 0,
            "articles_filtered": 0,
            "errors": [],
        }


def pipeline_config(**overrides):
    values = {
        "notion_api_key": "notion-key",
        "notion_database_id": "database-id",
        "notion_page_size": 100,
        "sync_published_after": None,
        "sync_scan_limit": 100,
        "ai_provider": "deepseek",
        "ai_worker_batch_size": 20,
        "event_dedup_enabled": False,
        "event_dedup_threshold": 0.96,
        "event_dedup_window_days": 7,
        "event_winner_margin_total": 2,
        "event_embedding_model": "model",
        "event_embedding_python": "/python",
        "event_embedding_cache": "/cache",
        "event_embedding_batch_size": 8,
        "event_embedding_threads": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_hourly_pipeline_enqueues_new_pages_without_calling_ai(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article_id = add_synced_article(storage, "queued")
    monkeypatch.setattr(runner, "Fetcher", FakeFetcher)
    monkeypatch.setattr(
        runner,
        "sync_articles_to_notion",
        lambda *args, **kwargs: {
            "success": True,
            "synced": 1,
            "pending": 0,
            "synced_article_ids": [article_id],
        },
    )

    def unexpected_ai_call(*args, **kwargs):
        raise AssertionError("hourly pipeline must not call DeepSeek")

    monkeypatch.setattr(runner, "enrich_articles_with_ai", unexpected_ai_call)

    result = runner.run_once(pipeline_config(), storage)

    assert result["ai"]["queued"] == 1
    assert storage.get_ai_queue_rows([article_id])[article_id]["status"] == "pending"


def test_event_clustering_happens_before_sync_and_same_batch_uses_one_canonical_page(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    feed = storage.add_feed("feed", "https://feed.test")
    first = storage.add_article(feed.id, "same story", "https://one", "A", "body", "body", None, None)
    second = storage.add_article(feed.id, "same story update", "https://two", "B", "body", "body", None, None)
    monkeypatch.setattr(runner, "Fetcher", FakeFetcher)
    embedder_args = {}

    class FakeEmbedder:
        def __init__(self, **kwargs):
            embedder_args.update(kwargs)

        def __call__(self, titles):
            return [[1.0] + [0.0] * 383 for _ in titles]

    monkeypatch.setattr(runner, "SubprocessTitleEmbedder", FakeEmbedder)

    def fake_sync(store, *args, **kwargs):
        members = store._get_conn().execute("SELECT event_id, article_id FROM event_members ORDER BY article_id").fetchall()
        assert len(members) == 2
        assert members[0]["event_id"] == members[1]["event_id"]
        assert [article.id for article in store.get_unsynced_articles()] == [first.id]
        assert {member["source"] for member in store.list_event_members(members[0]["event_id"])} == {"feed"}
        return {"success": True, "synced": 1, "pending": 0, "synced_article_ids": [first.id], "synced_page_map": {first.id: "page"}}

    monkeypatch.setattr(runner, "sync_articles_to_notion", fake_sync)
    result = runner.run_once(pipeline_config(event_dedup_enabled=True), storage)

    assert result["sync"]["synced"] == 1
    assert storage.get_article(first.id).notion_page_id == "page"
    assert storage.get_article(second.id).notion_page_id == "page"
    assert set(storage.get_ai_queue_rows([first.id, second.id])) == {first.id, second.id}
    assert str(embedder_args["python"]) == "/python"


def test_existing_canonical_event_attaches_presents_and_queues_new_candidate(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    feed = storage.add_feed("feed", "https://feed.test")
    old = storage.add_article(feed.id, "same story", "https://old", "A", "body", "body", None, None)
    event_id = storage.create_event("model")
    storage.append_event_member(
        event_id=event_id, article_id=old.id, title=old.title, url=old.url,
        source="feed", embedding=vector_to_blob([1.0] + [0.0] * 383),
    )
    storage.mark_event_canonical_synced(event_id, "existing-page")
    storage.mark_event_presented(event_id, 1, reset_succeeded=True)
    new = storage.add_article(
        feed.id, "same story update", "https://new", "B",
        "substantive " * 20, "substantive " * 20, None, None,
    )
    monkeypatch.setattr(runner, "Fetcher", FakeFetcher)

    class FakeEmbedder:
        def __init__(self, **kwargs):
            pass

        def __call__(self, titles):
            return [[1.0] + [0.0] * 383 for _ in titles]

    presentations = []

    class FakeNotionSync:
        def __init__(self, *args, **kwargs):
            pass

        def update_event_page(self, page_id, title, members, winner_id=None, reset_reading=False):
            presentations.append((page_id, title, members, winner_id, reset_reading))
            return {"success": True}

    monkeypatch.setattr(runner, "SubprocessTitleEmbedder", FakeEmbedder)
    monkeypatch.setattr(runner, "NotionSync", FakeNotionSync)

    def fake_sync(store, *args, **kwargs):
        assert store.get_unsynced_articles() == []
        return {"success": True, "synced": 0, "pending": 0, "synced_article_ids": [], "synced_page_map": {}}

    monkeypatch.setattr(runner, "sync_articles_to_notion", fake_sync)

    result = runner.run_once(pipeline_config(event_dedup_enabled=True), storage)

    assert storage.get_article(new.id).notion_page_id == "existing-page"
    assert result["ai"]["article_ids"] == [new.id]
    assert storage.get_ai_queue_rows([new.id])[new.id]["phase"] == "score"
    assert len(presentations) == 1
    assert presentations[0][0] == "existing-page"
    assert presentations[0][4] is True
    assert len(presentations[0][2]) == 2
    assert storage.claim_pending_event_presentations() == []


def test_singleton_event_is_presented_and_reset_is_acknowledged_only_after_notion_success(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    feed = storage.add_feed("feed", "https://feed.test")
    article = storage.add_article(feed.id, "single", "https://single", "A", "body", "body", None, None)
    event_id = storage.create_event("model")
    storage.append_event_member(
        event_id=event_id, article_id=article.id, title=article.title, url=article.url,
        source="feed", embedding=vector_to_blob([1.0] + [0.0] * 383),
    )
    storage.mark_event_canonical_synced(event_id, "page")
    calls = []

    class FakeNotionSync:
        def __init__(self, *args, **kwargs):
            pass

        def update_event_page(self, page_id, title, members, winner_id=None, reset_reading=False):
            calls.append((page_id, title, members, reset_reading))
            return {"success": True}

    monkeypatch.setattr(runner, "NotionSync", FakeNotionSync)
    runner._present_pending_events(storage, pipeline_config(event_dedup_enabled=True))

    assert len(calls) == 1
    assert calls[0][3] is True
    assert storage.claim_pending_event_presentations() == []


class FakeWorkerStorage:
    def __init__(self, claimed=None, states=None, queue_rows=None):
        self.claimed = claimed or []
        self.states = states or {}
        self.completed = []
        self.failed = []
        self.released = []
        self.claim_calls = 0
        self.queue_rows = queue_rows or {}

    def claim_ai_queue(self, limit):
        self.claim_calls += 1
        return AIQueueClaim("test-token" if self.claimed else None, self.claimed)

    def get_ai_prompt_config(self):
        return {
            "score_prompts": {"实用性": "score it"},
            "combined_prompt": "summarize it",
        }

    def get_ai_enrichment_states(self, article_ids, expected_fields):
        assert set(expected_fields) == {"实用性", "分类", "摘要", "金句"}
        return self.states

    def get_ai_queue_rows(self, article_ids):
        return {article_id: self.queue_rows[article_id] for article_id in article_ids if article_id in self.queue_rows}

    def complete_ai_queue(self, article_ids, claim_token):
        self.completed.extend(article_ids)

    def fail_ai_queue(self, article_ids, claim_token, error):
        self.failed.extend(article_ids)

    def release_ai_queue(self, article_ids, claim_token, error):
        self.released.extend(article_ids)


def test_process_lock_prevents_overlapping_workers(tmp_path):
    lock_path = str(tmp_path / "worker.lock")
    with runner._exclusive_ai_worker_lock(lock_path) as first:
        assert first is True
        with runner._exclusive_ai_worker_lock(lock_path) as second:
            assert second is False


def test_worker_does_not_claim_during_deepseek_peak(monkeypatch):
    storage = FakeWorkerStorage([1])
    peak = datetime(2026, 8, 17, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(
        runner,
        "enrich_articles_with_ai",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call AI")),
    )

    result = runner.run_ai_worker(pipeline_config(), storage, now=peak)

    assert result["reason"] == "DeepSeek peak window"
    assert storage.claim_calls == 0


def test_empty_ai_worker_exits_before_external_clients_are_used(monkeypatch):
    storage = FakeWorkerStorage([])
    idle = datetime(2026, 8, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(
        runner,
        "enrich_articles_with_ai",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call AI")),
    )

    result = runner.run_ai_worker(pipeline_config(), storage, now=idle)

    assert result["reason"] == "AI queue empty"
    assert storage.claim_calls == 1


def test_ai_worker_finalizes_each_claimed_article_from_persisted_field_states(monkeypatch):
    storage = FakeWorkerStorage(
        [1, 2, 3],
        {1: "completed", 2: "failed", 3: "pending"},
    )
    idle = datetime(2026, 8, 17, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(
        runner,
        "enrich_articles_with_ai",
        lambda *args, **kwargs: {"success": True, "failed_fields": 1},
    )

    result = runner.run_ai_worker(pipeline_config(), storage, now=idle)

    assert storage.completed == [1]
    assert storage.failed == [2]
    assert storage.released == [3]
    assert result["claimed"] == 3


def test_ai_worker_respects_event_phase_transitions_and_terminal_outcomes(monkeypatch):
    storage = FakeWorkerStorage(
        [1, 2, 3],
        {1: "pending", 2: "pending", 3: "pending"},
        {
            1: {"mode": "event", "phase": "meta", "status": "pending", "claim_token": None},
            2: {"mode": "event", "phase": "terminal", "status": "processing", "claim_token": "test-token"},
            3: {"mode": "event", "phase": "score", "status": "processing", "claim_token": "test-token"},
        },
    )
    idle = datetime(2026, 8, 17, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(runner, "enrich_articles_with_ai", lambda *args, **kwargs: {"success": True})

    result = runner.run_ai_worker(pipeline_config(), storage, now=idle)

    assert storage.completed == [2]
    assert storage.failed == []
    assert storage.released == [3]
    assert result["queue_completed"] == 1
    assert result["queue_released"] == 1
