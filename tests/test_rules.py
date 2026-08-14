from information_filter.models import InformationItem
from information_filter.processors.rules import KeywordFilter, MinimumLengthFilter, RegexFilter


def item(title: str = "", content: str = "") -> InformationItem:
    return InformationItem(id="1", title=title, content=content)


def test_keyword_include_and_exclude_are_case_insensitive() -> None:
    processor = KeywordFilter(include_any=["AI"], exclude_any=["sponsored"])
    assert processor.process(item("Practical ai guide", "useful")) is not None
    assert processor.process(item("AI", "Sponsored post")) is None
    assert processor.process(item("Unrelated", "text")) is None


def test_regex_filter() -> None:
    processor = RegexFilter(include=[r"python\s+3"], exclude=[r"beta"])
    assert processor.process(item(content="Python 3.12 is available")) is not None
    assert processor.process(item(content="Python 3 beta")) is None


def test_minimum_length() -> None:
    processor = MinimumLengthFilter(minimum=5)
    assert processor.process(item(content="12345")) is not None
    assert processor.process(item(content="1234")) is None
