"""Fetch records from a JSON HTTP endpoint."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from urllib.request import Request

from ..http import open_request
from ..interfaces import Source
from ..models import InformationItem


def _at_path(value: object, path: str) -> object:
    current = value
    for part in filter(None, path.split(".")):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"Response path not found: {path}")
        current = current[part]
    return current


class HTTPJSONSource(Source):
    def __init__(
        self,
        url: str,
        source: str = "http",
        items_path: str = "",
        token_env: str | None = None,
        token_header: str = "Authorization",
        token_prefix: str = "Bearer ",
        timeout: float = 30,
    ) -> None:
        self.url = url
        self.source = source
        self.items_path = items_path
        self.token_env = token_env
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.timeout = timeout

    def collect(self) -> Iterable[InformationItem]:
        headers = {"Accept": "application/json", "User-Agent": "automatic-information-filter/0.1"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValueError(f"Required environment variable is missing: {self.token_env}")
            headers[self.token_header] = f"{self.token_prefix}{token}"
        request = Request(self.url, headers=headers)
        with open_request(request, timeout=self.timeout) as response:
            payload = json.load(response)
        records = _at_path(payload, self.items_path) if self.items_path else payload
        if not isinstance(records, list):
            raise ValueError("HTTP JSON source must resolve to a list of objects")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Every HTTP record must be a JSON object")
            yield InformationItem.from_mapping(record, source=self.source)
