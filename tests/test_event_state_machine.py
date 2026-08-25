from pipeline.storage import AI_META_FIELDS, SCORING_PROMPT_KEYS, Storage
from types import SimpleNamespace

from pipeline import deepseek_enrichment
from pipeline.deepseek_enrichment import _build_pending_tasks

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def add_article(storage, title, url):
    feed = storage.get_feed_by_url("https://feed.test") or storage.add_feed("feed", "https://feed.test")
    return storage.add_article(feed.id, title, url, "author", "body", "body", None, None)


def attach(storage, event_id, article):
    storage.append_event_member(
        event_id=event_id,
        article_id=article.id,
        title=article.title,
        url=article.url,
        source="feed",
        embedding=b"",
    )


def test_clustering_queries_and_canonical_page_assignment_are_durable(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    first = add_article(storage, "first", "https://one.test")
    duplicate = add_article(storage, "duplicate", "https://two.test")
    event_id = storage.create_event(MODEL, event_id="event")
    attach(storage, event_id, first)
    attach(storage, event_id, duplicate)

    assert [row.id for row in storage.get_unclustered_unsynced_articles()] == []
    assert [row.id for row in storage.get_unsynced_articles()] == [first.id]

    assert storage.mark_event_canonical_synced(event_id, "page") == 2
    assert storage.get_event(event_id)["notion_page_id"] == "page"
    assert storage.get_article(first.id).notion_page_id == "page"
    assert storage.get_article(duplicate.id).notion_page_id == "page"
    assert {row["notion_page_id"] for row in storage.list_event_members(event_id)} == {"page"}


def test_event_presentation_reset_is_claimed_by_revision_and_acked_only_after_success(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article = add_article(storage, "first", "https://one.test")
    event_id = storage.create_event(MODEL, event_id="event")
    attach(storage, event_id, article)
    storage.set_event_page_id(event_id, "page")

    claim = storage.claim_pending_event_presentations()
    assert [(row["event_id"], row["revision"], row["reset_reading"]) for row in claim] == [
        (event_id, 1, True)
    ]
    assert storage.claim_pending_event_presentations()[0]["reset_reading"] is True

    assert storage.mark_event_presented(event_id, 1, reset_succeeded=False) is True
    retry = storage.claim_pending_event_presentations()[0]
    assert retry["reset_reading"] is True
    assert storage.mark_event_presented(event_id, 1, reset_succeeded=True) is True
    assert storage.claim_pending_event_presentations() == []

    second = add_article(storage, "second", "https://two.test")
    attach(storage, event_id, second)
    claim = storage.claim_pending_event_presentations()[0]
    assert claim["revision"] == 2
    assert claim["reset_reading"] is True


def complete_scores(storage, article_id, values):
    for field, value in zip(SCORING_PROMPT_KEYS, values):
        storage.upsert_ai_result_stub(article_id, "page", field, "score")
        storage.mark_ai_fields_completed(article_id, {field: str(value)}, "{}")


def test_score_decision_waits_for_six_valid_scores_and_applies_plus_two_margin(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    articles = [
        add_article(storage, name, f"https://{name}.test")
        for name in ("initial", "plus-one", "plus-two")
    ]
    event_id = storage.create_event(MODEL, event_id="event")
    for article in articles:
        attach(storage, event_id, article)
    storage.mark_event_canonical_synced(event_id, "page")

    complete_scores(storage, articles[0].id, [3, 3, 3, 3, 3, 3])
    complete_scores(storage, articles[1].id, [4, 3, 3, 3, 3, 3])
    # Incomplete challenger must not trigger a decision.
    for field in SCORING_PROMPT_KEYS[:5]:
        storage.upsert_ai_result_stub(articles[2].id, "page", field, "score")
        storage.mark_ai_fields_completed(articles[2].id, {field: "4"}, "{}")

    first = storage.decide_event_candidate(event_id, articles[0].id, margin=2)
    plus_one = storage.decide_event_candidate(event_id, articles[1].id, margin=2)
    incomplete = storage.decide_event_candidate(event_id, articles[2].id, margin=2)
    assert first["decision"] == "initial_winner"
    assert plus_one["decision"] == "loser"
    assert incomplete["decision"] == "blocked"
    assert storage.get_event(event_id)["current_winner_article_id"] == articles[0].id
    assert all(
        storage.get_ai_results_for_article(articles[1].id)[field]["status"] == "skipped"
        for field in AI_META_FIELDS
    )

    # Complete at +2 and retry: it becomes replacement_pending, not committed yet.
    last_field = SCORING_PROMPT_KEYS[-1]
    storage.upsert_ai_result_stub(articles[2].id, "page", last_field, "score")
    storage.mark_ai_fields_completed(articles[2].id, {last_field: "4"}, "{}")
    decision = storage.decide_event_candidate(event_id, articles[2].id, margin=2)
    assert decision["decision"] == "replacement_pending"
    assert storage.get_event(event_id)["current_winner_article_id"] == articles[0].id


def test_later_member_waits_for_initial_member_to_become_first_winner_without_burning_queue_claim(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    initial = add_article(storage, "initial", "https://initial")
    later = add_article(storage, "later", "https://later")
    event_id = storage.create_event(MODEL, "event-order")
    attach(storage, event_id, initial)
    attach(storage, event_id, later)
    storage.mark_event_canonical_synced(event_id, "page")
    storage.enqueue_ai_articles([later.id])
    storage.claim_ai_queue(1)
    complete_scores(storage, later.id, [5, 5, 5, 5, 5, 5])

    result = storage.decide_event_candidate(event_id, later.id)

    assert result["decision"] == "blocked"
    assert storage.get_event(event_id)["current_winner_article_id"] is None
    row = storage.get_ai_queue_rows([later.id])[later.id]
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["claim_token"] is None


def test_score_snapshot_schedules_event_info_refresh_without_another_read_reset(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article = add_article(storage, "first", "https://one.test")
    event_id = storage.create_event(MODEL, event_id="event")
    attach(storage, event_id, article)
    storage.mark_event_canonical_synced(event_id, "page")
    storage.mark_event_presented(event_id, 1, reset_succeeded=True)

    complete_scores(storage, article.id, [4, 4, 4, 4, 4, 4])
    storage.aggregate_event_member_scores(article.id)

    claim = storage.claim_pending_event_presentations()[0]
    assert claim["revision"] == 2
    assert claim["reset_reading"] is False


def test_event_ai_queue_persists_phase_event_and_operation_context(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article = add_article(storage, "first", "https://one.test")
    event_id = storage.create_event(MODEL, event_id="event")
    attach(storage, event_id, article)
    storage.mark_event_canonical_synced(event_id, "page")

    storage.enqueue_ai_articles([article.id])
    row = storage.get_ai_queue_rows([article.id])[article.id]
    assert row["phase"] == "score"
    assert row["mode"] == "event"
    assert row["event_id"] == event_id
    assert row["operation_id"] is not None

    storage.advance_event_ai_queue(article.id, "meta", "meta-op")
    row = storage.get_ai_queue_rows([article.id])[article.id]
    assert row["phase"] == "meta"
    assert row["operation_id"] == "meta-op"


def test_event_member_builds_score_only_then_meta_only_tasks(tmp_path):
    storage = Storage(str(tmp_path / "pipeline.db"))
    article = add_article(storage, "first", "https://one.test")
    event_id = storage.create_event(MODEL, event_id="event")
    attach(storage, event_id, article)
    storage.mark_event_canonical_synced(event_id, "page")
    storage.set_scoring_prompts({field: "score" for field in SCORING_PROMPT_KEYS})
    storage.set_combined_prompt("meta")
    storage.enqueue_ai_articles([article.id])

    tasks = _build_pending_tasks(storage, storage.get_article(article.id), 3)
    assert len(tasks) == 6
    assert {task.request_group for task in tasks} == {"score"}

    complete_scores(storage, article.id, [3, 3, 3, 3, 3, 3])
    storage.advance_event_ai_queue(article.id, "meta", "meta-op")
    tasks = _build_pending_tasks(storage, storage.get_article(article.id), 3)
    assert len(tasks) == 1
    assert tasks[0].field_names == AI_META_FIELDS


class FakeAIClient:
    provider_name = "fake"
    max_attempts = 1
    max_workers = 2

    def __init__(self, score=3):
        self.score = score
        self.scores = iter(score) if isinstance(score, list) else None
        self.calls = []

    def missing_credentials_reason(self):
        return None

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.calls.append(user_prompt)
        if "分类、摘要、金句" in user_prompt:
            return '{"分类":"AI","摘要":"summary","金句":"quote"}', {}
        score = next(self.scores) if self.scores is not None else self.score
        return f'{{"score":{score}}}', {}

    def is_rate_limit_error(self, exc):
        return False

    def should_retry(self, exc, attempt):
        return False


class FakeEventSync:
    instances = []
    default_fail = False

    def __init__(self, *args, **kwargs):
        self.piecemeal = []
        self.representatives = []
        self.fail_apply = self.default_fail
        self.__class__.instances.append(self)

    def update_rich_text_properties(self, page_id, values):
        self.piecemeal.append((page_id, values))
        return []

    def apply_representative(self, page_id, article, members, values, winner_id):
        self.representatives.append((page_id, article, members, values, winner_id))
        return {"success": not self.fail_apply, "error": "notion failed" if self.fail_apply else None}


def ai_config():
    return SimpleNamespace(ai_enrichment_batch_size=10)


def prepare_event_ai(storage, title="candidate"):
    feed = storage.get_feed_by_url("https://feed.test") or storage.add_feed("feed", "https://feed.test")
    article = storage.add_article(
        feed.id, title, f"https://{title}.test", "author", "substantive " * 20,
        "substantive " * 20, None, None,
    )
    event_id = storage.create_event(MODEL)
    attach(storage, event_id, article)
    storage.mark_event_canonical_synced(event_id, "page")
    storage.set_scoring_prompts({field: "score" for field in SCORING_PROMPT_KEYS})
    storage.set_combined_prompt("meta")
    storage.enqueue_ai_articles([article.id])
    return event_id, article


def test_event_ai_scores_locally_then_retries_meta_and_applies_once(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    event_id, article = prepare_event_ai(storage)
    client = FakeAIClient(score=3)
    FakeEventSync.instances = []
    monkeypatch.setattr(deepseek_enrichment, "build_ai_client", lambda config: client)
    monkeypatch.setattr(deepseek_enrichment, "NotionSync", FakeEventSync)

    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [article.id])

    first_sync = FakeEventSync.instances[-1]
    assert first_sync.piecemeal == []
    assert first_sync.representatives == []
    assert storage.get_ai_queue_rows([article.id])[article.id]["phase"] == "meta"
    assert storage.get_event(event_id)["current_winner_article_id"] == article.id
    assert len(client.calls) == 6

    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [article.id])

    second_sync = FakeEventSync.instances[-1]
    assert second_sync.piecemeal == []
    assert len(second_sync.representatives) == 1
    _, _, members, values, winner_id = second_sync.representatives[0]
    assert winner_id == article.id
    assert len(members) == 1
    assert "score" not in members[0]
    assert members[0]["score_total"] == 18
    assert members[0]["score_count"] == 6
    assert set(values) == set(SCORING_PROMPT_KEYS + AI_META_FIELDS)
    rows = storage.get_ai_results_for_article(article.id)
    assert all(rows[field]["push_status"] == "completed" for field in SCORING_PROMPT_KEYS + AI_META_FIELDS)
    assert storage.get_event(event_id)["state"] == "active"


def test_plus_two_challenger_commits_only_after_atomic_notion_success(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    event_id, winner = prepare_event_ai(storage, "winner")
    complete_scores(storage, winner.id, [3] * 6)
    storage.decide_event_candidate(event_id, winner.id)
    storage.mark_event_ai_queue_terminal(winner.id, "fixture")

    feed = storage.get_feed_by_url("https://feed.test")
    challenger = storage.add_article(
        feed.id, "challenger", "https://challenger.test", "author",
        "substantive " * 20, "substantive " * 20, None, None,
    )
    attach(storage, event_id, challenger)
    storage.mark_event_canonical_synced(event_id, "page")
    storage.enqueue_ai_articles([challenger.id])
    client = FakeAIClient(score=[4, 4, 3, 3, 3, 3])
    FakeEventSync.instances = []
    FakeEventSync.default_fail = True
    monkeypatch.setattr(deepseek_enrichment, "build_ai_client", lambda config: client)
    monkeypatch.setattr(deepseek_enrichment, "NotionSync", FakeEventSync)

    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [challenger.id])
    assert storage.get_event(event_id)["current_winner_article_id"] == winner.id
    assert storage.get_ai_queue_rows([challenger.id])[challenger.id]["phase"] == "meta"

    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [challenger.id])
    failed_sync = FakeEventSync.instances[-1]
    assert len(failed_sync.representatives) == 1
    assert failed_sync.piecemeal == []
    assert storage.get_event(event_id)["current_winner_article_id"] == winner.id
    assert storage.get_event(event_id)["state"] == "replacement_pending"

    FakeEventSync.default_fail = False
    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [challenger.id])
    successful_sync = FakeEventSync.instances[-1]
    assert len(successful_sync.representatives) == 1
    assert storage.get_event(event_id)["current_winner_article_id"] == challenger.id
    assert storage.get_event(event_id)["replacement_count"] == 1
    assert storage.get_event(event_id)["state"] == "active"
    assert storage.get_ai_queue_rows([challenger.id])[challenger.id]["phase"] == "terminal"


def test_plus_one_event_candidate_runs_only_scores_and_finishes_as_loser(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    event_id, winner = prepare_event_ai(storage, "winner-low")
    complete_scores(storage, winner.id, [3] * 6)
    storage.decide_event_candidate(event_id, winner.id)
    storage.mark_event_ai_queue_terminal(winner.id, "fixture")

    feed = storage.get_feed_by_url("https://feed.test")
    challenger = storage.add_article(
        feed.id, "plus-one", "https://plus-one.test", "author",
        "substantive " * 20, "substantive " * 20, None, None,
    )
    attach(storage, event_id, challenger)
    storage.mark_event_canonical_synced(event_id, "page")
    storage.enqueue_ai_articles([challenger.id])
    client = FakeAIClient(score=[4, 3, 3, 3, 3, 3])
    FakeEventSync.instances = []
    FakeEventSync.default_fail = False
    monkeypatch.setattr(deepseek_enrichment, "build_ai_client", lambda config: client)
    monkeypatch.setattr(deepseek_enrichment, "NotionSync", FakeEventSync)

    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [challenger.id])

    assert len(client.calls) == 6
    assert FakeEventSync.instances[-1].representatives == []
    assert storage.get_event(event_id)["current_winner_article_id"] == winner.id
    assert storage.get_ai_queue_rows([challenger.id])[challenger.id]["phase"] == "terminal"
    rows = storage.get_ai_results_for_article(challenger.id)
    assert all(rows[field]["status"] == "skipped" for field in AI_META_FIELDS)


def test_thin_event_member_still_runs_required_six_score_phase(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path / "pipeline.db"))
    _event_id, article = prepare_event_ai(storage, "thin")
    with storage.transaction() as conn:
        conn.execute("UPDATE articles_raw SET content_raw='tiny', content_text='tiny' WHERE id=?", (article.id,))
    client = FakeAIClient(score=3)
    FakeEventSync.instances = []
    monkeypatch.setattr(deepseek_enrichment, "build_ai_client", lambda config: client)
    monkeypatch.setattr(deepseek_enrichment, "NotionSync", FakeEventSync)

    deepseek_enrichment.enrich_articles_with_ai(storage, ai_config(), "key", "db", [article.id])

    assert len(client.calls) == 6
    assert storage.get_ai_queue_rows([article.id])[article.id]["phase"] == "meta"
