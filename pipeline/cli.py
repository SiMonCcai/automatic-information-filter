"""
Command-line interface for pipeline management.
"""

import argparse
import sys
from datetime import datetime

from .config import Config
from .storage import Storage
from .runner import run_once


def cmd_feed_list(args):
    """List all feeds."""
    storage = Storage(args.db)
    feeds = storage.list_feeds(enabled_only=args.enabled)

    if not feeds:
        print("No feeds found.")
        return

    print(f"\n{'ID':<5} {'Enabled':<8} {'Name':<30} {'URL'}")
    print("-" * 80)
    for feed in feeds:
        enabled_str = "✓" if feed.enabled else "✗"
        print(f"{feed.id:<5} {enabled_str:<8} {feed.name:<30} {feed.url}")

        if feed.last_fetched_at:
            print(f"      Last fetched: {feed.last_fetched_at}")
        if feed.fetch_error:
            print(f"      Error: {feed.fetch_error}")


def cmd_feed_add(args):
    """Add a new feed."""
    storage = Storage(args.db)

    # Check for duplicate URL
    existing = storage.get_feed_by_url(args.url)
    if existing:
        print(f"Error: Feed with URL '{args.url}' already exists (ID: {existing.id})")
        sys.exit(1)

    feed = storage.add_feed(args.name, args.url)
    print(f"Added feed: {feed.name} (ID: {feed.id})")
    print(f"  URL: {feed.url}")


def cmd_feed_disable(args):
    """Disable a feed."""
    storage = Storage(args.db)

    feed = storage.get_feed(args.id)
    if not feed:
        print(f"Error: Feed with ID {args.id} not found")
        sys.exit(1)

    storage.set_feed_enabled(args.id, False)
    print(f"Disabled feed: {feed.name} (ID: {feed.id})")


def cmd_sync_once(args):
    """Run a single sync iteration."""
    config = Config.from_env()
    if args.db:
        config.db_path = args.db

    storage = Storage(config.db_path)

    print(f"Starting sync at {datetime.now()}")
    print("-" * 50)

    result = run_once(config, storage, dry_run=args.dry_run)

    print("-" * 50)
    print(f"\nResults:")
    print(f"  Articles fetched: {result['fetch'].get('articles_added', 0)}")
    print(f"  Articles cleaned: {result['clean'].get('cleaned', 0)}")
    print(f"  Articles synced:  {result['sync'].get('synced', 0)}")

    if result['fetch'].get('errors'):
        print(f"\nErrors:")
        for error in result['fetch']['errors'][:5]:
            print(f"  - {error}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="RSS Pipeline CLI")
    parser.add_argument("--db", default="pipeline.db", help="Database path")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # feed list
    list_parser = subparsers.add_parser("feed", help="Manage feeds")
    list_subparsers = list_parser.add_subparsers(dest="feed_cmd")

    feed_list = list_subparsers.add_parser("list", help="List feeds")
    feed_list.add_argument("--enabled", action="store_true", help="Show only enabled feeds")
    feed_list.set_defaults(func=cmd_feed_list)

    # feed add
    feed_add = list_subparsers.add_parser("add", help="Add a feed")
    feed_add.add_argument("--name", required=True, help="Feed name")
    feed_add.add_argument("--url", required=True, help="Feed URL")
    feed_add.set_defaults(func=cmd_feed_add)

    # feed disable
    feed_disable = list_subparsers.add_parser("disable", help="Disable a feed")
    feed_disable.add_argument("--id", type=int, required=True, help="Feed ID")
    feed_disable.set_defaults(func=cmd_feed_disable)

    # sync once
    sync_parser = subparsers.add_parser("sync", help="Run sync")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_cmd")

    sync_once = sync_subparsers.add_parser("once", help="Run single sync")
    sync_once.add_argument("--dry-run", action="store_true", help="Dry run (no Notion sync)")
    sync_once.set_defaults(func=cmd_sync_once)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
