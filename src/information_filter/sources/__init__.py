"""Built-in input adapters."""

from .http_json import HTTPJSONSource
from .json_file import JSONFileSource
from .rss import RSSSource

__all__ = ["HTTPJSONSource", "JSONFileSource", "RSSSource"]
