from scripts.embed_titles import embed_titles


def test_embed_titles_uses_safe_low_memory_settings():
    calls = {}

    class FakeModel:
        def embed(self, titles, batch_size):
            calls["titles"] = titles
            calls["batch_size"] = batch_size
            return [[1.0, 2.0] for _ in titles]

    def factory(**kwargs):
        calls.update(kwargs)
        return FakeModel()

    result = embed_titles(
        ["标题A", "Title B"],
        model_factory=factory,
        model_name="test-model",
        cache_dir="/persistent/cache",
        threads=1,
        batch_size=8,
    )

    assert result == [[1.0, 2.0], [1.0, 2.0]]
    assert calls["model_name"] == "test-model"
    assert calls["cache_dir"] == "/persistent/cache"
    assert calls["threads"] == 1
    assert calls["batch_size"] == 8