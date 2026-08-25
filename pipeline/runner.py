"""
Main pipeline runner - orchestrates fetch, clean, and sync steps.
"""

import argparse
import fcntl
import logging
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .ai_schedule import is_deepseek_idle_time
from .cleaner import HTMLCleaner
from .config import Config
from .deepseek_enrichment import enrich_articles_with_ai
from .fetcher import Fetcher
from .event_dedup import EventCandidate, SubprocessTitleEmbedder, TitleEventMatcher
from .notion_sync import NotionSync, sync_articles_to_notion
from .storage import AI_META_FIELDS, SCORING_PROMPT_KEYS, Storage

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _present_pending_events(storage: Storage, config: Config) -> None:
    """Apply retryable event-page effects; acknowledge only successful updates."""
    event_syncer = NotionSync(config.notion_api_key, config.notion_database_id)
    for claim in storage.claim_pending_event_presentations():
        members = claim.get("members") or []
        winner_id = claim.get("current_winner_article_id")
        if winner_id is None and members:
            winner_id = members[0]["article_id"]
        winner = storage.get_article(winner_id) if winner_id is not None else None
        title = winner.title if winner is not None else (members[0]["title"] if members else "")
        applied = event_syncer.update_event_page(
            claim["notion_page_id"], title, members,
            winner_id=winner_id,
            reset_reading=bool(claim.get("reset_reading")),
        )
        if applied.get("success"):
            storage.mark_event_presented(
                claim["event_id"], int(claim["revision"]),
                reset_succeeded=bool(claim.get("reset_reading")),
            )
        else:
            logger.warning(
                "Event presentation failed for %s: %s",
                claim["event_id"], applied.get("error"),
            )


def run_once(config: Config, storage: Storage, dry_run: bool = False) -> dict[str, any]:
    """Run ingestion under a process-wide exclusive lock."""
    with _exclusive_ingestion_lock() as acquired:
        if not acquired:
            return {"success": True, "skipped": True, "reason": "Ingestion already running"}
        return _run_once_locked(config, storage, dry_run=dry_run)


def _run_once_locked(config: Config, storage: Storage, dry_run: bool = False) -> dict[str, any]:
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
                   f"{fetch_result['articles_skipped']} skipped, "
                   f"{fetch_result.get('articles_filtered', 0)} filtered")

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
                    tx.execute(
                        """
                        INSERT INTO article_content_archive (
                            url, title, content_text, first_seen_at, last_seen_at
                        )
                        SELECT
                            url,
                            title,
                            ?,
                            COALESCE(synced_at, fetched_at, datetime('now')),
                            COALESCE(synced_at, fetched_at, datetime('now'))
                        FROM articles_raw
                        WHERE id = ?
                        ON CONFLICT(url) DO UPDATE SET
                            title = COALESCE(excluded.title, article_content_archive.title),
                            content_text = COALESCE(excluded.content_text, article_content_archive.content_text),
                            last_seen_at = excluded.last_seen_at
                        """,
                        (cleaned, article_id)
                    )
                cleaned_count += 1
            except Exception as e:
                logger.warning(f"Error cleaning article {article_id}: {e}")

        results["clean"] = {"cleaned": cleaned_count}
        logger.info(f"Cleaned {cleaned_count} articles")

        # Cluster only new/unsynced rows before any page can be created. Embedding
        # failure is fail-open inside TitleEventMatcher.
        matches = []
        event_synced_article_ids: list[int] = []
        if getattr(config, "event_dedup_enabled", False) and not dry_run:
            unclustered = storage.get_unclustered_unsynced_articles()
            embedder = SubprocessTitleEmbedder(
                python=Path(config.event_embedding_python),
                cache_dir=Path(config.event_embedding_cache),
                model_name=config.event_embedding_model,
                batch_size=config.event_embedding_batch_size,
                threads=config.event_embedding_threads,
            )
            feed_names = {feed.id: feed.name for feed in storage.list_feeds()}
            matches = TitleEventMatcher(
                storage,
                embedder=embedder,
                threshold=config.event_dedup_threshold,
                model_name=config.event_embedding_model,
                window_days=config.event_dedup_window_days,
                expected_dimension=384,
            ).match_and_store([
                EventCandidate(
                    article.id,
                    article.title,
                    article.url,
                    feed_names.get(article.feed_id) or article.author,
                )
                for article in unclustered
            ])
            results["events"] = {"classified": len(matches)}
            for event_id in dict.fromkeys(match.event_id for match in matches):
                event = storage.get_event(event_id)
                if not event or not event.get("notion_page_id"):
                    continue
                pending_ids = [
                    member["article_id"]
                    for member in storage.list_event_members(event_id)
                    if not member.get("synced_at")
                ]
                if pending_ids:
                    storage.mark_event_canonical_synced(event_id, event["notion_page_id"])
                    event_synced_article_ids.extend(pending_ids)

        # Step 3: Sync to Notion (if configured)
        logger.info("=" * 50)
        logger.info("STEP 3: Syncing to Notion...")
        logger.info("=" * 50)

        if config.notion_api_key and config.notion_database_id:
            sync_result = sync_articles_to_notion(
                storage,
                config.notion_api_key,
                config.notion_database_id,
                batch_size=config.notion_page_size,
                dry_run=dry_run,
                sync_published_after=config.sync_published_after,
                sync_scan_limit=config.sync_scan_limit,
                event_mode_enabled=config.event_dedup_enabled,
            )
            results["sync"] = sync_result

            # A single initial member was created; attach its returned page to
            # every duplicate member in one local transaction.
            if getattr(config, "event_dedup_enabled", False) and sync_result.get("success"):
                page_map = sync_result.get("synced_page_map", {})
                for article_id, page_id in page_map.items():
                    member = storage._get_conn().execute(
                        "SELECT event_id FROM event_members WHERE article_id=?", (article_id,)
                    ).fetchone()
                    if member:
                        storage.mark_event_canonical_synced(member["event_id"], page_id)
                canonical_ids = []
                for article_id in sync_result.get("synced_article_ids", []):
                    member = storage._get_conn().execute(
                        "SELECT event_id FROM event_members WHERE article_id=?", (article_id,)
                    ).fetchone()
                    if member:
                        canonical_ids.extend(row["article_id"] for row in storage.list_event_members(member["event_id"]))
                    else:
                        canonical_ids.append(article_id)
                sync_result["synced_article_ids"] = list(dict.fromkeys(canonical_ids))

            if getattr(config, "event_dedup_enabled", False) and not dry_run:
                sync_result["synced_article_ids"] = list(dict.fromkeys(
                    [*sync_result.get("synced_article_ids", []), *event_synced_article_ids]
                ))
                _present_pending_events(storage, config)

            if sync_result.get("success"):
                logger.info(f"Sync complete: {sync_result['synced']} synced, "
                           f"{sync_result.get('pending', 0)} pending")
            else:
                logger.error(f"Sync failed: {sync_result.get('error', 'Unknown error')}")
        else:
            logger.info("Notion not configured, skipping sync")
            results["sync"] = {"skipped": True, "reason": "Not not configured"}

        # Step 4: Queue AI enrichment for the separate off-peak worker
        logger.info("=" * 50)
        logger.info("STEP 4: Queueing newly synced pages for deferred AI enrichment...")
        logger.info("=" * 50)

        if config.notion_api_key and config.notion_database_id and not dry_run:
            synced_article_ids = results.get("sync", {}).get("synced_article_ids", [])
            queued = storage.enqueue_ai_articles(synced_article_ids)
            results["ai"] = {
                "success": True,
                "queued": queued,
                "article_ids": synced_article_ids,
                "reason": "Deferred to off-peak AI worker",
            }
            logger.info("AI enrichment deferred: %s newly queued articles", queued)
        else:
            results["ai"] = {"skipped": True, "reason": "Dry run or Notion not configured"}

        # Step 5: Cleanup old synced articles
        logger.info("=" * 50)
        logger.info("STEP 5: Cleanup old synced articles...")
        logger.info("=" * 50)

        deleted_count = storage.cleanup_synced_articles(keep_days=30)
        results["cleanup"] = {"deleted": deleted_count, "keep_days": 30}
        logger.info(f"Cleanup complete: deleted {deleted_count} synced articles older than 30 days")

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


def _expected_ai_fields(storage: Storage) -> list[str]:
    prompt_cfg = storage.get_ai_prompt_config()
    expected = [
        field_name
        for field_name in SCORING_PROMPT_KEYS
        if str((prompt_cfg.get("score_prompts") or {}).get(field_name, "")).strip()
    ]
    if str(prompt_cfg.get("combined_prompt") or "").strip():
        expected.extend(AI_META_FIELDS)
    return expected


@contextmanager
def _ai_claim_heartbeat(
    storage: Storage,
    article_ids: list[int],
    claim_token: str,
    interval_seconds: float = 300,
):
    """Keep a worker lease alive so stale recovery cannot overlap live work."""
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(interval_seconds):
            renewed = storage.renew_ai_queue_claim(article_ids, claim_token)
            if renewed != len(article_ids):
                logger.error("AI queue heartbeat lost claim ownership; renewed=%s", renewed)
                return

    thread = threading.Thread(target=renew, name="ai-queue-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


@contextmanager
def _exclusive_ingestion_lock(path: str = "/tmp/rss-ingestion-internal.lock"):
    """Prevent overlapping fetch/cluster/create flows."""
    with open(path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_ai_worker_lock(path: str = "/tmp/rss-ai-worker-internal.lock"):
    """Prevent any two live worker processes from enriching concurrently."""
    with open(path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_ai_worker(
    config: Config,
    storage: Storage,
    *,
    now: datetime | None = None,
) -> dict[str, any]:
    with _exclusive_ai_worker_lock() as acquired:
        if not acquired:
            logger.info("AI worker skipped: another worker holds the process lock")
            return {"success": True, "skipped": True, "reason": "AI worker already running"}
        return _run_ai_worker_locked(config, storage, now=now)


def _run_ai_worker_locked(
    config: Config,
    storage: Storage,
    *,
    now: datetime | None = None,
) -> dict[str, any]:
    """Process one persistent AI queue batch when provider billing is off-peak."""
    if config.ai_provider == "deepseek" and not is_deepseek_idle_time(now):
        logger.info("AI worker skipped: DeepSeek peak window")
        return {"success": True, "skipped": True, "reason": "DeepSeek peak window"}

    claim = storage.claim_ai_queue(config.ai_worker_batch_size)
    article_ids = claim.article_ids
    if not article_ids:
        logger.info("AI worker skipped: queue empty")
        return {"success": True, "skipped": True, "reason": "AI queue empty"}

    logger.info("AI worker claimed %s articles", len(article_ids))
    try:
        with _ai_claim_heartbeat(storage, article_ids, claim.token):
            ai_result = enrich_articles_with_ai(
                storage,
                config,
                config.notion_api_key,
                config.notion_database_id,
                article_ids=article_ids,
            )
            states = storage.get_ai_enrichment_states(article_ids, _expected_ai_fields(storage))
            queue_rows = storage.get_ai_queue_rows(article_ids)
            completed_ids: list[int] = []
            failed_ids: list[int] = []
            pending_ids: list[int] = []
            for article_id in article_ids:
                row = queue_rows.get(article_id, {})
                if row.get("mode") == "event":
                    # Advancing score -> meta deliberately clears the old claim and
                    # creates a fresh pending retry. The old worker must not ack it.
                    if row.get("status") != "processing" or row.get("claim_token") != claim.token:
                        continue
                    if row.get("phase") == "terminal":
                        completed_ids.append(article_id)
                    elif states.get(article_id) == "failed":
                        failed_ids.append(article_id)
                    else:
                        pending_ids.append(article_id)
                elif states.get(article_id) == "completed":
                    completed_ids.append(article_id)
                elif states.get(article_id) == "failed":
                    failed_ids.append(article_id)
                else:
                    pending_ids.append(article_id)
            storage.complete_ai_queue(completed_ids, claim.token)
            storage.fail_ai_queue(failed_ids, claim.token, "One or more AI fields exhausted retries")
            storage.release_ai_queue(pending_ids, claim.token, "AI fields remain pending")
            result = {
                **ai_result,
                "claimed": len(article_ids),
                "queue_completed": len(completed_ids),
                "queue_failed": len(failed_ids),
                "queue_released": len(pending_ids),
            }
            logger.info(
                "AI worker complete: claimed=%s completed=%s failed=%s released=%s",
                len(article_ids),
                len(completed_ids),
                len(failed_ids),
                len(pending_ids),
            )
            return result
    except Exception as exc:
        storage.release_ai_queue(article_ids, claim.token, str(exc))
        logger.exception("AI worker failed; claimed articles returned to queue")
        return {"success": False, "claimed": len(article_ids), "error": str(exc)}


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
    parser.add_argument("--once", action="store_true", help="Run ingestion once and exit")
    parser.add_argument("--ai-only", action="store_true", help="Process one off-peak AI queue batch and exit")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (no Notion sync)")
    parser.add_argument("--db", default="pipeline.db", help="Database path")
    parser.add_argument("--interval", type=int, default=60, help="Continuous mode interval (minutes)")

    args = parser.parse_args()

    # Load config
    config = Config.from_env()
    if args.db:
        config.db_path = args.db

    storage = Storage(config.db_path)

    if args.ai_only:
        result = run_ai_worker(config, storage)
        sys.exit(0 if result.get("error") is None else 1)
    if args.once:
        result = run_once(config, storage, dry_run=args.dry_run)
        sys.exit(0 if result.get("error") is None else 1)
    else:
        run_continuous(config, args.interval)


if __name__ == "__main__":
    main()
