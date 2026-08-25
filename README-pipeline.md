# RSS Pipeline

A modular RSS feed fetching, cleaning, and Notion synchronization pipeline.

## Features

- **RSS Feed Fetching**: Parse RSS/Atom feeds with error handling
- **Deduplication**: URL/content deduplication plus optional title-semantic event clustering
- **HTML Cleaning**: Convert HTML content to plain text
- **Notion Sync**: Push articles to Notion database
- **SQLite Storage**: Persistent storage for feeds, articles, and sync jobs
- **Web Admin Panel**: Simple web interface for feed and sync management
- **Editable AI Scoring Prompts**: Maintain per-dimension scoring prompts in the admin panel
- **Two-stage AI Enrichment**: Event members are scored first; only a candidate beating the representative by the configured margin runs 分类 / 摘要 / 金句
- **Stable Event Pages**: Multiple reports of one event reuse one Notion page while SQLite keeps every source, score, similarity, and replacement decision

## Installation

```bash
# Install dependencies
pip install feedparser beautifulsoup4 notion-client flask

# Or with requirements
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required for Notion sync:
- `NOTION_API_KEY`: Your Notion integration token
- `NOTION_DATABASE_ID`: Target database ID

Required for admin panel:
- `ADMIN_USERNAME`: Admin username (HTTP Basic Auth)
- `ADMIN_PASSWORD`: Admin password

Required for AI enrichment:
- `AI_PROVIDER`: `deepseek` or `glm`
- `AI_ENRICHMENT_BATCH_SIZE`: How many synced articles to scan per run

Provider-specific credentials:
- DeepSeek: `DEEPSEEK_API_KEY`
- GLM: `GLM_API_KEY`

Optional:
- `PIPELINE_DB_PATH`: Database path (default: pipeline.db)
- `DEEPSEEK_BASE_URL`: Default `https://api.deepseek.com`
- `DEEPSEEK_MODEL`: Default `deepseek-v4-flash`
- `DEEPSEEK_TIMEOUT`: Request timeout seconds
- `DEEPSEEK_MAX_RETRIES`: Per-call retry limit (default 3)
- `DEEPSEEK_MAX_CONCURRENCY`: Parallel request workers for DeepSeek (default 7)
- `GLM_BASE_URL`: Default `https://open.bigmodel.cn/api/paas/v4`
- `GLM_MODEL`: Default `glm-4.7`
- `GLM_TIMEOUT`: Request timeout seconds
- `GLM_MAX_RETRIES`: Per-call retry limit (default 5)
- `GLM_MAX_CONCURRENCY`: Concurrent in-flight GLM requests (default 2)
- `GLM_MIN_REQUEST_INTERVAL`: Minimum seconds between starting GLM requests (default 1.2)
- `GLM_RATE_LIMIT_COOLDOWN`: Extra cooldown after GLM 1302/1303/1305 or HTTP 429 responses (default 8.0)
- `EVENT_DEDUP_ENABLED`: Enable title-semantic event clustering (default `false`)
- `EVENT_DEDUP_THRESHOLD`: Cosine threshold (default `0.96`)
- `EVENT_DEDUP_WINDOW_DAYS`: Recent event search window (default `7`)
- `EVENT_WINNER_MARGIN_TOTAL`: Required six-score total advantage before winner metadata enrichment (default `2`)

See [`docs/event-dedup-implementation.md`](docs/event-dedup-implementation.md)
for the durable state machine, crash recovery rules, and deployment parameters.

## Database Schema

### Tables

- **feeds**: RSS feed configurations
  - id, name, url, enabled, created_at, last_fetched_at, fetch_error, default_author

- **articles_raw**: Fetched articles
  - id, feed_id, title, url, author, content_raw, content_text, published_at,
    fetched_at, fingerprint, synced_at, notion_page_id

- **sync_jobs**: Sync job tracking
  - id, started_at, finished_at, status, articles_synced, error_message

## Admin Panel

Start the web admin panel:

```bash
python3 -m pipeline.admin
```

Or with custom host/port:

```bash
python3 -m pipeline.admin --host 0.0.0.0 --port 8080
```

The admin panel provides:
- **Feed Management**: Add, delete, enable/disable feeds
- **Author Mapping**: Set default author per feed (used when article author is empty)
- **Sync Control**: Trigger manual sync, view sync status and history
- **AI Scoring Prompt Config**: Edit prompt text for 实用性、客观性、是否营销内容、有趣性、独特性、信息密度
- **Combined Metadata Prompt**: Configure one shared AI prompt for 分类、摘要、金句

Default access at http://127.0.0.1:5000 (requires HTTP Basic Auth).

## Usage

### CLI

```bash
# List feeds
python3 -m pipeline.cli feed list

# Add a feed
python3 -m pipeline.cli feed add --name "Example Feed" --url "https://example.com/rss"

# Disable a feed
python3 -m pipeline.cli feed disable --id 1

# Run single sync
python3 -m pipeline.cli sync once

# Dry run (no Notion sync)
python3 -m pipeline.cli sync once --dry-run
```

### Runner

```bash
# Run hourly ingestion and queue newly synced pages for AI
python3 -m pipeline.runner --once

# Process one queued AI batch (DeepSeek peak windows are blocked in code)
python3 -m pipeline.runner --ai-only

# Continuous ingestion mode (default 60 minute interval)
python3 -m pipeline.runner

# Custom interval
python3 -m pipeline.runner --interval 30
```

### Switch AI provider

```bash
# Use DeepSeek
export AI_PROVIDER=deepseek

# Use GLM
export AI_PROVIDER=glm
export GLM_API_KEY=your_glm_api_key_here
```

## Cron Setup

Keep ingestion hourly. Run the AI queue every ten minutes only during DeepSeek
off-peak windows. This example assumes the server itself uses UTC; DeepSeek
billing windows are evaluated in Beijing time by the application as a second
safety check.

```cron
# Hourly RSS/Notion ingestion
5 * * * * cd /path/to/workspace && set -a && . ./.env && set +a && python3 -m pipeline.runner --once >> pipeline.log 2>&1

# Beijing off-peak windows translated to UTC; flock prevents overlapping workers
*/10 0,4-5,10-23 * * * cd /path/to/workspace && set -a && . ./.env && set +a && flock -n /tmp/rss-ai-worker.lock python3 -m pipeline.runner --ai-only >> pipeline.log 2>&1
```

## Notion Database Setup

Your Notion database must have the following properties:

| Property Name | Type       | Required |
|--------------|------------|----------|
| 文章名称       | Title      | Yes      |
| 网址          | URL        | Yes      |
| 作者          | Text       | Yes      |
| 内容          | Text       | Yes      |
| 发布时间       | Date       | No       |

## Data Flow

1. **Fetch**: Parse RSS feeds and extract articles
2. **Deduplicate**: Skip articles by URL (same feed) or content fingerprint
3. **Cluster events (optional)**: Embed titles, match recent events, and reuse the canonical page
4. **Clean**: Convert HTML content to plain text
5. **Sync**: Push unsynced articles to Notion

## Deduplication Strategy

1. **Primary**: URL uniqueness per feed
2. **Fallback**: SHA-256 fingerprint of `title + published_at + author`

This handles:
- Duplicate URLs in the same feed
- Same content across different feeds
- Articles with changed URLs but same content
