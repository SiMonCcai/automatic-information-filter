# Automatic Information Filter

[中文](README.md)

A pluggable framework for automatic information filtering.

It defines one pipeline:

```text
sources → normalization → filtering / processing → sinks
```

Each stage is replaceable. Collect from feeds, APIs, scrapers, message streams, or databases. Filter with keywords, regular expressions, conventional algorithms, local programs, or remote services. Publish to files, databases, web services, email, chat, or a knowledge base.

The project is not tied to a particular source, model provider, or presentation layer.

## What it is useful for

- Combine several sources and keep only the items worth reading
- Cluster reports from different sources into one stable event record
- Tag, score, summarize, or classify incoming information
- Send normalized results to another system for presentation
- Swap sources, decision logic, and outputs without rebuilding the pipeline
- Run on a schedule with cron, containers, or a workflow platform

## Built-in components

| Stage | Built-ins |
|---|---|
| Sources | JSON / JSONL files, JSON HTTP APIs, RSS / Atom |
| Processing | Keywords, regular expressions, minimum length, generic HTTP decisions |
| Sinks | JSONL, SQLite, standard output, HTTP endpoints |
| State | In-memory and persistent SQLite deduplication |

The built-ins are a practical starting point. Any Python class implementing `Source`, `Processor`, `Sink`, or `StateStore` can be loaded from the configuration file.

## Quick start

Python 3.10 or newer is required.

```bash
git clone https://github.com/SiMonCcai/automatic-information-filter.git
cd automatic-information-filter
python -m venv .venv
. .venv/bin/activate
pip install -e .

aif validate -c examples/pipeline.toml
aif run -c examples/pipeline.toml
```

The example reads fictional records, applies length and keyword filters, then writes accepted items to:

```text
examples/output/accepted.jsonl
```

A SQLite state file prevents the same items from being processed again.

## Configure a pipeline

Configuration uses TOML. This example collects from a JSON API, applies two local filters, and posts accepted items to any HTTP receiver:

```toml
[[sources]]
type = "http_json"
url = "https://api.example.com/items"
items_path = "data.items"
source = "example-api"
token_env = "SOURCE_API_TOKEN"

[[processors]]
type = "minimum_length"
field = "content"
minimum = 100

[[processors]]
type = "keyword"
include_any = ["AI", "automation"]
exclude_any = ["sponsored"]
fields = ["title", "content"]

[[sinks]]
type = "http"
url = "https://receiver.example.com/inbox"
token_env = "OUTPUT_API_TOKEN"

[state]
type = "sqlite"
path = ".aif/state.db"
```

Secrets come from environment variables. The configuration stores variable names, never secret values.

## Connect any decision service

The `http_decision` processor works with any service that accepts and returns JSON. That service may use a rules engine, a conventional classifier, a local inference server, or a third-party API.

```toml
[[processors]]
type = "http_decision"
url = "http://127.0.0.1:9000/decide"
token_env = "DECISION_SERVICE_TOKEN"
```

The request body is a normalized information item. The service returns:

```json
{
  "accept": true,
  "annotations": {
    "category": "technology",
    "score": 0.91
  }
}
```

`accept` controls whether the item continues. `annotations` travel with it through later processors and sinks.

## Duplicate-event handling

Fingerprint deduplication only detects identical items. When several sources describe the same event with different titles, add a stateful event-clustering Processor:

1. generate replaceable semantic embeddings from titles only;
2. cosine-match them against a recent event window;
3. attach matches to the existing event and reuse its stable output ID;
4. persist every member, source URL, similarity, score, and representative change;
5. score every member first, but run expensive summarization or classification only for candidates that clearly beat the current representative;
6. fail open when embedding fails and continue with an independent event.

The embedding provider, threshold, time window, state store, and sink remain adapter choices. The core framework does not require a specific implementation. See [Event-level deduplication](docs/event-deduplication.md) for the durable state machine, idempotency, and crash-recovery design.

## Write an adapter

```python
from collections.abc import Iterable

from information_filter.interfaces import Source
from information_filter.models import InformationItem


class MySource(Source):
    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def collect(self) -> Iterable[InformationItem]:
        yield InformationItem(
            id="item-1",
            title="Example",
            content="Collected by a custom source",
            source="my-source",
        )
```

Load it with `module:class` syntax:

```toml
[[sources]]
type = "my_package.sources:MySource"
endpoint = "https://example.com"
```

A `Processor` returns the item to keep it and `None` to reject it. `Sink.write()` receives all items that passed the processor chain. See [`examples/my_plugins.py`](examples/my_plugins.py) for a complete minimal example.

## Commands

```bash
aif validate -c pipeline.toml
aif run -c pipeline.toml
aif plugins
```

A cron entry can call the same one-shot command:

```cron
15 * * * * cd /path/to/project && . .venv/bin/activate && aif run -c pipeline.toml
```

## Processing semantics

1. Each source yields normalized `InformationItem` objects.
2. The state store creates source-scoped fingerprints and skips completed items.
3. Processors run in configuration order. Returning `None` rejects an item.
4. Accepted items are written to every sink.
5. Items enter the deduplication state only after every sink succeeds.

Deduplication records the input fingerprint before processing. A processor may rewrite the ID, source, or URL without breaking the next run's duplicate check. Rejected items are not recorded, so changed rules can evaluate them again later.

Several sinks cannot form a transaction across different systems. Sink adapters should use the item fingerprint or a business ID for idempotent writes so retries do not create duplicate records.

## Development

```bash
pip install -e '.[dev]'
ruff check .
pytest
```

The project supports Python 3.10, 3.11, and 3.12. Release verification currently runs on Python 3.10.

## Security

- Do not commit API keys, tokens, production databases, logs, or real source data.
- Use `token_env` to reference environment variables.
- To prevent credential leaks, built-in HTTP adapters reject redirects that change the host, scheme, or port.
- HTTP adapters treat their target URLs as trusted configuration. Restrict internal addresses and allowed hosts if configuration is exposed through a public UI.
- Custom plugins are local Python code. Load only modules you trust.

## License

[MIT](LICENSE)
