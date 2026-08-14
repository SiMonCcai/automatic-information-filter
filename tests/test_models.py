from information_filter.models import InformationItem


def test_mapping_keeps_unknown_fields_as_metadata() -> None:
    item = InformationItem.from_mapping(
        {"title": "Hello", "url": "https://example.com/1", "category": "news"},
        source="api",
    )
    assert item.id == "https://example.com/1"
    assert item.source == "api"
    assert item.metadata == {"category": "news"}


def test_fingerprint_is_stable_and_source_scoped() -> None:
    first = InformationItem(id="1", url="https://example.com/1", source="one")
    same = InformationItem(id="other", url="https://example.com/1", source="one")
    other_source = InformationItem(id="1", url="https://example.com/1", source="two")
    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != other_source.fingerprint
