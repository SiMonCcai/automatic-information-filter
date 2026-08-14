"""Built-in output adapters."""

from .http import HTTPSink
from .jsonl import JSONLinesSink
from .sqlite import SQLiteSink
from .stdout import StdoutSink

__all__ = ["HTTPSink", "JSONLinesSink", "SQLiteSink", "StdoutSink"]
