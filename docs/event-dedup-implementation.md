# Event-level title dedup implementation

## Locked behavior

- Enable behind `EVENT_DEDUP_ENABLED`; production activation only after schema/test verification.
- Embed title only with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, FastEmbed helper venv, batch 8, threads 1.
- Compare against event members seen in the previous 7 days; auto-join at cosine >= 0.96. Fail open when embedding/matching fails.
- One event owns one stable Notion page. New events create a normal page; later members do not create pages.
- Every member gets all six existing 1-5 scores. A challenger replaces the winner only when all six are complete and score total is at least current winner total + 2.
- Non-winners never run 分类/摘要/金句. Winners run all three, then replace the same Notion page in one property update.
- Every newly attached member is added immediately to `事件信息` as a clickable original link (待评分 until scores complete), updates `【事件·N】`, and clears `阅读状态` exactly once. Later score/meta updates for the same member update `事件信息` but do not clear reading again.
- SQLite is authoritative and retains denormalized event/member history beyond the 30-day raw article cleanup.

## State machine

1. Ingest/clean new article.
2. Batch-embed newly inserted article IDs and assign event.
3. New event: initial member remains eligible for own Notion page.
4. Existing event: member is excluded from own-page sync; if canonical page exists, mark article terminal/synced to canonical page, append event info/reset unread, enqueue score-only.
5. After normal Notion sync, attach returned page IDs to newly created events, append any same-batch members, and enqueue every event member in score-only phase.
6. AI worker uses score-only first for every event member, including the initial page-owning member. No event member runs meta in parallel with scoring and no score is pushed piecemeal to Notion.
7. When all six scores are complete, persist the total and decide: the initial member becomes the initial winner; a later member below margin becomes terminal loser; a qualifying challenger enters winner-meta. If current winner scoring is still pending, block without consuming retry attempts.
8. Only a selected winner runs meta. After all three meta fields are stored locally, perform one complete page update; then commit the winner transition idempotently.
9. Notion update failure leaves durable pending state and retries idempotently. Reading reset is effectively-once: mark `reading_reset_done=1` only after an explicit success response, and never reset during later score/meta refreshes.
10. A single ingestion process lock prevents concurrent page creation. If create succeeds but local persistence is interrupted, retry must recover the page by the initial article URL before creating another; no additional hidden Notion event-id field is allowed.

## Required tests

- schema migration/idempotent member append/history survives cleanup
- float32 embedding BLOB/cosine/recent-window/highest match/batch self-match/fail-open
- one-member title vs `【事件·N】`
- all member links are real Notion rich text links
- new-member update clears checkbox once; score refresh does not
- score parser rejects bool; incomplete scores cannot compare
- candidate does not schedule meta before score decision and does not push scores
- +1 total does not replace, +2 replaces; tie deterministic old winner
- replacement sends title/URL/content/scores/meta/event info in one page update
- missing Notion schema and network failures leave retryable local state
- runner clusters before sync and duplicate candidates never create own pages
- legacy baseline tests remain green
