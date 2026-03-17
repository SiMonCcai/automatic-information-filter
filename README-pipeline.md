# RSS Pipeline

A modular RSS feed fetching, cleaning, and Notion synchronization pipeline.

## Features

- **RSS Feed Fetching**: Parse RSS/Atom feeds with error handling
- **Deduplication**: URL-based deduplication with content fingerprint fallback
- **HTML Cleaning**: Convert HTML content to plain text
- **Notion Sync**: Push articles to Notion database
- **SQLite Storage**: Persistent storage for feeds, articles, and sync jobs
- **Web Admin Panel**: Simple web interface for feed and sync management

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

Optional:
- `PIPELINE_DB_PATH`: Database path (default: pipeline.db)
- `SYNC_PUBLISHED_AFTER`: only sync articles with `published_at >= YYYY-MM-DD` (e.g. `2026-03-01`)
- `SYNC_SCAN_LIMIT`: number of unsynced rows to scan before date filtering (default: `100`)

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
# Run once
python3 -m pipeline.runner --once

# Continuous mode (default 60 minute interval)
python3 -m pipeline.runner

# Custom interval
python3 -m pipeline.runner --interval 30
```

## Cron Setup

Run hourly via cron:

```cron
# Run RSS pipeline every hour
0 * * * * cd /path/to/workspace && . .env && python3 -m pipeline.runner --once >> pipeline.log 2>&1
```

Or with the CLI:

```cron
0 * * * * cd /path/to/workspace && . .env && python3 -m pipeline.cli sync once >> pipeline.log 2>&1
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
3. **Clean**: Convert HTML content to plain text
4. **Sync**: Push eligible unsynced articles to Notion
5. **Cleanup**: Delete synced rows older than 7 days (`synced_at IS NOT NULL`)

## Unsynced Count Semantics

- `unsynced_count` in admin means `synced_at IS NULL` raw count.
- When `SYNC_PUBLISHED_AFTER` is set, some unsynced rows may be intentionally skipped by date filter and remain unsynced.
- If you want the dashboard number to represent only immediately syncable items, add a separate `eligible_count` metric (not enabled by default).

## Deduplication Strategy

1. **Primary**: URL uniqueness per feed
2. **Fallback**: SHA-256 fingerprint of `title + published_at + author`

This handles:
- Duplicate URLs in the same feed
- Same content across different feeds
- Articles with changed URLs but same content
