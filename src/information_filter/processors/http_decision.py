"""Delegate filtering or enrichment to any JSON-speaking HTTP service."""

from __future__ import annotations

import json
import os
from urllib.request import Request

from ..http import open_request
from ..interfaces import Processor
from ..models import InformationItem


class HTTPDecisionProcessor(Processor):
    """POST an item and expect {"accept": bool, "annotations": {...}}."""

    def __init__(
        self,
        url: str,
        token_env: str | None = None,
        token_header: str = "Authorization",
        token_prefix: str = "Bearer ",
        timeout: float = 60,
    ) -> None:
        self.url = url
        self.token_env = token_env
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.timeout = timeout

    def process(self, item: InformationItem) -> InformationItem | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValueError(f"Required environment variable is missing: {self.token_env}")
            headers[self.token_header] = f"{self.token_prefix}{token}"
        request = Request(
            self.url,
            data=json.dumps(item.to_dict(), ensure_ascii=False).encode(),
            headers=headers,
            method="POST",
        )
        with open_request(request, timeout=self.timeout) as response:
            decision = json.load(response)
        if not isinstance(decision, dict) or not isinstance(decision.get("accept"), bool):
            raise ValueError("Decision endpoint must return a JSON object with boolean 'accept'")
        annotations = decision.get("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError("'annotations' must be a JSON object")
        item.annotations.update(annotations)
        return item if decision["accept"] else None
