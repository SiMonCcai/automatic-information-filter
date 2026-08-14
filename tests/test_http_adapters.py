import io
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from information_filter.http import SameOriginRedirectHandler
from information_filter.models import InformationItem
from information_filter.processors.http_decision import HTTPDecisionProcessor
from information_filter.sinks.http import HTTPSink
from information_filter.sources.http_json import HTTPJSONSource


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_http_json_source_maps_nested_records(monkeypatch) -> None:
    payload = json.dumps({"data": {"items": [{"id": "1", "title": "hello"}]}}).encode()
    monkeypatch.setattr(
        "information_filter.sources.http_json.open_request",
        lambda request, timeout: Response(payload),
    )
    items = list(HTTPJSONSource("https://example.com", items_path="data.items").collect())
    assert [(item.id, item.title) for item in items] == [("1", "hello")]


def test_http_decision_adds_annotations(monkeypatch) -> None:
    payload = json.dumps({"accept": True, "annotations": {"score": 0.9}}).encode()
    monkeypatch.setattr(
        "information_filter.processors.http_decision.open_request",
        lambda request, timeout: Response(payload),
    )
    item = InformationItem(id="1")
    result = HTTPDecisionProcessor("https://example.com/decide").process(item)
    assert result is item
    assert item.annotations == {"score": 0.9}


def test_http_sink_posts_a_batch(monkeypatch) -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(json.loads(request.data))
        return Response(b"{}")

    monkeypatch.setattr("information_filter.sinks.http.open_request", open_request)
    item = InformationItem(id="1")
    assert HTTPSink("https://example.com/inbox").write([item]) == 1
    assert calls[0][0]["id"] == "1"


def test_http_decision_rejects_non_boolean_accept(monkeypatch) -> None:
    payload = json.dumps({"accept": "false"}).encode()
    monkeypatch.setattr(
        "information_filter.processors.http_decision.open_request",
        lambda request, timeout: Response(payload),
    )
    with pytest.raises(ValueError, match="boolean 'accept'"):
        HTTPDecisionProcessor("https://example.com/decide").process(InformationItem(id="1"))


@pytest.mark.parametrize(
    "new_url",
    [
        "https://other.example.com/items",
        "http://api.example.com/items",
        "https://api.example.com:8443/items",
    ],
)
def test_redirect_handler_rejects_cross_origin_redirect(new_url: str) -> None:
    handler = SameOriginRedirectHandler()
    request = Request("https://api.example.com/items", headers={"Authorization": "Bearer secret"})
    with pytest.raises(HTTPError, match="cross-origin redirect"):
        handler.redirect_request(
            request,
            io.BytesIO(),
            302,
            "Found",
            {},
            new_url,
        )


def test_redirect_handler_allows_same_origin_redirect() -> None:
    handler = SameOriginRedirectHandler()
    request = Request("https://api.example.com/items", headers={"Authorization": "Bearer secret"})
    redirected = handler.redirect_request(
        request,
        io.BytesIO(),
        302,
        "Found",
        {},
        "https://api.example.com/v2/items",
    )
    assert redirected.full_url == "https://api.example.com/v2/items"
