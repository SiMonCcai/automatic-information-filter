from pipeline.config import Config


def test_event_dedup_defaults_are_conservative():
    config = Config()

    assert config.event_dedup_enabled is False
    assert config.event_dedup_threshold == 0.96
    assert config.event_dedup_window_days == 7
    assert config.event_winner_margin_total == 2
    assert config.event_embedding_batch_size == 8
    assert config.event_embedding_threads == 1
    assert config.event_embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_event_dedup_config_loads_environment(monkeypatch):
    monkeypatch.setenv("EVENT_DEDUP_ENABLED", "true")
    monkeypatch.setenv("EVENT_DEDUP_THRESHOLD", "0.97")
    monkeypatch.setenv("EVENT_DEDUP_WINDOW_DAYS", "3")
    monkeypatch.setenv("EVENT_WINNER_MARGIN_TOTAL", "3")
    monkeypatch.setenv("EVENT_EMBEDDING_BATCH_SIZE", "4")
    monkeypatch.setenv("EVENT_EMBEDDING_THREADS", "2")
    monkeypatch.setenv("EVENT_EMBEDDING_PYTHON", "/custom/python")
    monkeypatch.setenv("EVENT_EMBEDDING_CACHE", "/custom/cache")

    config = Config.from_env()

    assert config.event_dedup_enabled is True
    assert config.event_dedup_threshold == 0.97
    assert config.event_dedup_window_days == 3
    assert config.event_winner_margin_total == 3
    assert config.event_embedding_batch_size == 4
    assert config.event_embedding_threads == 2
    assert config.event_embedding_python == "/custom/python"
    assert config.event_embedding_cache == "/custom/cache"
