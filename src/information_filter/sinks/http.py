"""Publish accepted items to a JSON HTTP endpoint."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from urllib.request import Request

from ..http import open_request
from ..interfaces import Sink
from ..models import InformationItem


class HTTPSink(Sink):
    def __init__(
        self,
        url: str,
        token_env: str | None = None,
        token_header: str = "Authorization",
        token_prefix: str = "Bearer ",
        timeout: float = 30,
    ) -> None:
        self.url = url
        self.token_env = token_env
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.timeout = timeout

    def write(self, items: Sequence[InformationItem]) -> int:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValueError(f"Required environment variable is missing: {self.token_env}")
            headers[self.token_header] = f"{self.token_prefix}{token}"
        body = json.dumps([item.to_dict() for item in items], ensure_ascii=False).encode()
        request = Request(self.url, data=body, headers=headers, method="POST")
        with open_request(request, timeout=self.timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Output endpoint returned HTTP {response.status}")
        return len(items)
