"""
Main pipeline runner - orchestrates fetch, clean, and sync steps.
"""

import argparse
import logging
import sys
from datetime import datetime

from .cleaner import HTMLCleaner
from .config import Config
from .fetcher import Fetcher
from .notion_sync import sync_articles_to_notion
from .storage import Storage


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def run_once(config: Config, storage: Storage, dry_run: bool = False) -> dict[str, any]:
    """
    Run a single pipeline iteration.
    Returns summary dict.
    """
    logger.info("Starting pipeline run...")
    start_time = datetime.now()

    # Create sync job
    job = storage.create_sync_job()
    logger.info(f"Created sync job #{job.id}")

    results = {
        "job_id": job.id,
        "fetch": {},
        "clean": {},
        "sync": {},
    }

    try:
        # Step 1: Fetch articles
        logger.info("=" * 50)
        logger.info("STEP 1: Fetching articles from feeds...")
        logger.info("=" * 50)

        fetcher = Fetcher(storage, config)
        fetch_result = fetcher.fetch_all(enabled_only=True)
        results["fetch"] = fetch_result

        logger.info(f"Fetch complete: {fetch_result['articles_added']} added, "
                   f"{fetch_result['articles_skipped']} skipped")

        if fetch_result["errors"]:
            logger.warning(f"Fetch errors: {len(fetch_result['errors'])}")
            for error in fetch_result["errors"][:5]:
                logger.warning(f"  - {error}")

        # Step 2: Clean HTML content
        logger.info("=" * 50)
        logger.info("STEP 2: Cleaning HTML content...")
        logger.info("=" * 50)

        cleaner = HTMLCleaner()

        # Get articles that need cleaning (no content_text)
        conn = storage._get_conn()
        cursor = conn.execute("""
            SELECT id, content_raw
            FROM articles_raw
            WHERE content_raw IS NOT NULL
            AND (content_text IS NULL OR content_text = '')
            LIMIT 1000
        """)
        to_clean = cursor.fetchall()

        cleaned_count = 0
        for row in to_clean:
            article_id, content_raw = row
            try:
                cleaned = cleaner.clean(content_raw)
                with storage.transaction() as tx:
                    tx.execute(
                        "UPDATE articles_raw SET content_text = ? WHERE id = ?",
                        (cleaned, article_id)
                    )
                cleaned_count += 1
            except Exception as e:
                logger.warning(f"Error cleaning article {article_id}: {e}")

        results["clean"] = {"cleaned": cleaned_count}
        logger.info(f"Cleaned {cleaned_count} articles")

        # Step 3: Sync to Notion (if configured)
        logger.info("=" * 50)
        logger.info("STEP 3: Syncing to Notion...")
        logger.info("=" * 50)

        if config.notion_api_key and config.notion_database_id:
            sync_result = sync_articles_to_notion(
                storage,
                config.notion_api_key,
                config.notion_database_id,
                batch_size=100,
                dry_run=dry_run,
                sync_published_after=config.sync_published_after,
                sync_scan_limit=config.sync_scan_limit,
            )
            results["sync"] = sync_result

            if sync_result.get("success"):
                logger.info(f"Sync complete: {sync_result['synced']} synced, "
                           f"{sync_result.get('pending', 0)} pending")
            else:
                logger.error(f"Sync failed: {sync_result.get('error', 'Unknown error')}")
        else:
            logger.info("Notion not configured, skipping sync")
            results["sync"] = {"skipped": True, "reason": "Not not configured"}

        # Step 4: Cleanup old synced articles
        logger.info("=" * 50)
        logger.info("STEP 4: Cleanup old synced articles...")
        logger.info("=" * 50)

        deleted_count = storage.cleanup_synced_articles(keep_days=7)
        results["cleanup"] = {"deleted": deleted_count, "keep_days": 7}
        logger.info(f"Cleanup complete: deleted {deleted_count} synced articles older than 7 days")

        # Finish job
        elapsed = (datetime.now() - start_time).total_seconds()
        storage.finish_sync_job(
            job.id,
            status="completed",
            articles_synced=results["sync"].get("synced", 0),
        )
        logger.info("=" * 50)
        logger.info(f"Pipeline run completed in {elapsed:.1f}s")
        logger.info("=" * 50)

    except Exception as e:
        logger.error(f"Pipeline run failed: {e}", exc_info=True)
        storage.finish_sync_job(job.id, status="failed", articles_synced=0, error=str(e))
        results["error"] = str(e)

    return results


def run_continuous(config: Config, interval_minutes: int = 60):
    """Run pipeline continuously."""
    import time

    logger.info(f"Starting continuous mode (interval: {interval_minutes} minutes)")
    storage = Storage(config.db_path)

    while True:
        run_once(config, storage)
        logger.info(f"Next run in {interval_minutes} minutes...")
        time.sleep(interval_minutes * 60)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="RSS Pipeline Runner")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (no Notion sync)")
    parser.add_argument("--db", default="pipeline.db", help="Database path")
    parser.add_argument("--interval", type=int, default=60, help="Continuous mode interval (minutes)")

    args = parser.parse_args()

    # Load config
    config = Config.from_env()
    if args.db:
        config.db_path = args.db

    storage = Storage(config.db_path)

    if args.once:
        result = run_once(config, storage, dry_run=args.dry_run)
        sys.exit(0 if result.get("error") is None else 1)
    else:
        run_continuous(config, args.interval)


if __name__ == "__main__":
    main()
