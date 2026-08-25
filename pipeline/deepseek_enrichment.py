import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional
from urllib import error, request

from .config import Config
from .notion_sync import NotionSync
from .storage import AI_ALL_FIELDS, AI_META_FIELDS, SCORING_PROMPT_KEYS, Article, Storage

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
METADATA_ONLY_LINE_RE = re.compile(
    r"^(?:article\s+url|comments?\s+url|points?|#\s*comments?|source|link|read\s+more)\s*:\s*.*$",
    re.IGNORECASE,
)
PURE_METADATA_VALUE_RE = re.compile(r"^(?:#\s*comments?\s*:\s*\d+|points?\s*:\s*\d+)$", re.IGNORECASE)
MIN_SUBSTANTIVE_TEXT_CHARS = 80
GLM_RATE_LIMIT_ERROR_CODES = {1302, 1303, 1305}


@dataclass
class TaskSpec:
    field_names: list[str]
    request_group: str
    prompt_text: str


@dataclass
class AIHTTPError(Exception):
    provider: str
    message: str
    status_code: Optional[int] = None
    error_code: Optional[int] = None
    retry_after: Optional[float] = None

    def __str__(self) -> str:
        details = [self.provider, self.message]
        if self.status_code is not None:
            details.append(f"http={self.status_code}")
        if self.error_code is not None:
            details.append(f"code={self.error_code}")
        return " | ".join(details)


class RequestPacer:
    def __init__(self, max_concurrency: int, min_request_interval: float):
        self._semaphore = threading.BoundedSemaphore(max(1, max_concurrency))
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._min_request_interval = max(0.0, min_request_interval)

    def acquire(self) -> None:
        self._semaphore.acquire()
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    wait_seconds = self._next_request_at - now
                    if wait_seconds <= 0:
                        self._next_request_at = now + self._min_request_interval
                        return
                time.sleep(min(wait_seconds, 0.5))
        except Exception:
            self._semaphore.release()
            raise

    def release(self) -> None:
        self._semaphore.release()

    def cooldown(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._next_request_at = max(self._next_request_at, now + seconds)


class AIClient:
    provider_name = "ai"

    def __init__(self, model: str, timeout: int, max_attempts: int, max_workers: int):
        self.model = model
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.max_workers = max(1, max_workers)

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        disable_thinking: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError

    def retry_sleep(self, attempt: int, exc: Exception) -> None:
        base_delay = min(2 ** (attempt - 1), 6)
        jitter = random.uniform(0, 0.4)
        time.sleep(base_delay + jitter)

    def is_rate_limit_error(self, exc: Exception) -> bool:
        return False

    def should_retry(self, exc: Exception, attempt: int) -> bool:
        return True

    def missing_credentials_reason(self) -> Optional[str]:
        return None


class OpenAICompatibleClient(AIClient):
    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int,
        max_attempts: int,
        max_workers: int,
        pacer: RequestPacer | None = None,
        provider_rate_limit_error_codes: set[int] | None = None,
        provider_cooldown_seconds: float = 0.0,
    ):
        super().__init__(model=model, timeout=timeout, max_attempts=max_attempts, max_workers=max_workers)
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.pacer = pacer
        self.provider_rate_limit_error_codes = provider_rate_limit_error_codes or set()
        self.provider_cooldown_seconds = max(0.0, provider_cooldown_seconds)

    def missing_credentials_reason(self) -> Optional[str]:
        if not self.api_key:
            return f"{self.provider_name.upper()} API key not configured"
        return None

    def _build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        disable_thinking: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if disable_thinking and self.provider_name == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _parse_error_response(self, raw: bytes | str | None) -> tuple[Optional[int], str]:
        if raw is None:
            return None, "Unknown provider error"
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None, text.strip() or "Unknown provider error"

        error_block = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error_block, dict):
            code = error_block.get("code")
            message = error_block.get("message") or error_block.get("msg") or text
        else:
            code = parsed.get("code") if isinstance(parsed, dict) else None
            message = parsed.get("message") if isinstance(parsed, dict) else text

        try:
            numeric_code = int(code) if code is not None and str(code).strip() else None
        except (TypeError, ValueError):
            numeric_code = None
        return numeric_code, str(message).strip() or "Unknown provider error"

    def _open(self, req: request.Request) -> str:
        if self.pacer:
            self.pacer.acquire()
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read()
            code, message = self._parse_error_response(raw)
            retry_after_header = exc.headers.get("Retry-After") if exc.headers else None
            retry_after = None
            if retry_after_header:
                try:
                    retry_after = float(retry_after_header)
                except ValueError:
                    retry_after = None
            if code in self.provider_rate_limit_error_codes and self.pacer and self.provider_cooldown_seconds:
                self.pacer.cooldown(self.provider_cooldown_seconds)
            raise AIHTTPError(
                provider=self.provider_name,
                message=message,
                status_code=exc.code,
                error_code=code,
                retry_after=retry_after,
            ) from exc
        finally:
            if self.pacer:
                self.pacer.release()

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        disable_thinking: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        payload = self._build_payload(
            system_prompt,
            user_prompt,
            disable_thinking=disable_thinking,
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        raw = self._open(req)
        parsed = json.loads(raw)
        content = parsed["choices"][0]["message"]["content"]
        return content, parsed

    def retry_sleep(self, attempt: int, exc: Exception) -> None:
        if isinstance(exc, AIHTTPError) and exc.retry_after:
            delay = exc.retry_after
        elif isinstance(exc, AIHTTPError) and exc.error_code in self.provider_rate_limit_error_codes:
            delay = min(self.provider_cooldown_seconds + (attempt - 1) * 2, 30)
        else:
            delay = min(2 ** (attempt - 1), 6)
        delay += random.uniform(0, 0.6)
        if self.pacer and isinstance(exc, AIHTTPError) and exc.error_code in self.provider_rate_limit_error_codes:
            self.pacer.cooldown(delay)
        time.sleep(delay)

    def is_rate_limit_error(self, exc: Exception) -> bool:
        return isinstance(exc, AIHTTPError) and exc.error_code in self.provider_rate_limit_error_codes

    def should_retry(self, exc: Exception, attempt: int) -> bool:
        if not isinstance(exc, AIHTTPError):
            return True
        if exc.error_code in self.provider_rate_limit_error_codes:
            return True
        if exc.retry_after:
            return True
        if exc.status_code is not None and exc.status_code >= 500:
            return True
        return False


def build_ai_client(config: Config) -> AIClient:
    provider = (config.ai_provider or "deepseek").strip().lower()
    if provider == "deepseek":
        return OpenAICompatibleClient(
            provider_name="deepseek",
            base_url=config.deepseek_base_url,
            api_key=config.deepseek_api_key or "",
            model=config.deepseek_model,
            timeout=config.deepseek_timeout,
            max_attempts=config.deepseek_max_retries,
            max_workers=config.deepseek_max_concurrency,
        )
    if provider == "glm":
        return OpenAICompatibleClient(
            provider_name="glm",
            base_url=config.glm_base_url,
            api_key=config.glm_api_key or "",
            model=config.glm_model,
            timeout=config.glm_timeout,
            max_attempts=config.glm_max_retries,
            max_workers=config.glm_max_concurrency,
            pacer=RequestPacer(
                max_concurrency=config.glm_max_concurrency,
                min_request_interval=config.glm_min_request_interval,
            ),
            provider_rate_limit_error_codes=GLM_RATE_LIMIT_ERROR_CODES,
            provider_cooldown_seconds=config.glm_rate_limit_cooldown,
        )
    raise ValueError(f"Unsupported AI provider: {config.ai_provider}")


def _strip_json_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_score_response(text: str, field_name: str) -> dict[str, str]:
    data = json.loads(_strip_json_fence(text))
    if not isinstance(data, dict):
        raise ValueError(f"{field_name} score response must be a JSON object")
    score = data.get("score")
    if isinstance(score, str) and score.isdigit():
        score = int(score)
    if type(score) is not int or not (1 <= score <= 5):
        raise ValueError(f"{field_name} score must be integer 1-5")
    return {field_name: str(score)}


def _parse_score_bundle_response(text: str, field_names: list[str]) -> dict[str, str]:
    data = json.loads(_strip_json_fence(text))
    if not isinstance(data, dict):
        raise ValueError("Score response must be a JSON object")
    result: dict[str, str] = {}
    for field_name in field_names:
        score = data.get(field_name)
        if isinstance(score, str) and score.isdigit():
            score = int(score)
        if type(score) is not int or not (1 <= score <= 5):
            raise ValueError(f"{field_name} score must be integer 1-5")
        result[field_name] = str(score)
    return result


def _parse_meta_response(text: str) -> dict[str, str]:
    data = json.loads(_strip_json_fence(text))
    result: dict[str, str] = {}
    for field in AI_META_FIELDS:
        value = data.get(field, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{field} must be string")
        result[field] = value.strip()
    return result


def _article_payload(article: Article) -> str:
    body = (article.content_text or article.content_raw or "").strip()
    return f"标题：{article.title}\n\n正文：\n{body}"


def _article_content_eligibility(article: Article) -> tuple[bool, str]:
    body = (article.content_text or article.content_raw or "").strip()
    if not body:
        return False, "ai_skipped: empty_content"

    raw_lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not raw_lines:
        return False, "ai_skipped: empty_content"

    metadata_lines = 0
    substantive_lines: list[str] = []
    url_count = len(URL_RE.findall(body))

    for line in raw_lines:
        lowered = line.lower()
        if METADATA_ONLY_LINE_RE.match(lowered) or PURE_METADATA_VALUE_RE.match(lowered):
            metadata_lines += 1
            continue

        cleaned_line = URL_RE.sub(" ", line)
        cleaned_line = re.sub(r"\s+", " ", cleaned_line).strip(" :-•\t")
        if cleaned_line:
            substantive_lines.append(cleaned_line)

    cleaned_text = "\n".join(substantive_lines)
    cleaned_chars = len(re.sub(r"\s+", "", cleaned_text))

    if metadata_lines >= max(2, len(raw_lines) - 1) and cleaned_chars < MIN_SUBSTANTIVE_TEXT_CHARS:
        return False, "ai_skipped: metadata_only_content"

    if url_count >= 1 and cleaned_chars < MIN_SUBSTANTIVE_TEXT_CHARS:
        return False, "ai_skipped: insufficient_text_after_url_strip"

    return True, ""


def _score_user_prompt(field_name: str, base_prompt: str, article: Article) -> str:
    return (
        f"{(base_prompt or '').strip()}\n\n"
        "你只会收到一篇文章的标题和正文。"
        f"请只针对字段“{field_name}”打分。"
        "请严格只返回 JSON："
        '{"score": 1}'
        "。score 必须是 1 到 5 的整数，不要输出任何额外文字、解释、Markdown 或代码块。\n\n"
        f"{_article_payload(article)}"
    )


def _meta_user_prompt(base_prompt: str, article: Article) -> str:
    return (
        f"{(base_prompt or '').strip()}\n\n"
        "你只会收到一篇文章的标题和正文。"
        "请严格只返回 JSON，包含以下 3 个字段：分类、摘要、金句。"
        "字段值都必须是字符串，不要输出任何额外文字、解释、Markdown 或代码块。"
        "JSON 格式示例："
        '{"分类": "...", "摘要": "...", "金句": "..."}'
        "。\n\n"
        f"{_article_payload(article)}"
    )


def _push_with_retry(
    storage: Storage,
    syncer: NotionSync,
    article_id: int,
    page_id: str,
    values: dict[str, str],
    max_attempts: int,
) -> None:
    field_names = list(values.keys())
    last_error = None
    for attempt in range(1, max_attempts + 1):
        storage.mark_ai_fields_push_processing(article_id, field_names)
        try:
            skipped = syncer.update_rich_text_properties(page_id, values)
            if skipped:
                raise ValueError(f"Notion properties missing or not rich_text: {', '.join(skipped)}")
            storage.mark_ai_fields_pushed(article_id, field_names)
            return
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "AI push failed for article %s fields %s attempt %s/%s: %s",
                article_id,
                field_names,
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 6) + random.uniform(0, 0.4))
    storage.mark_ai_fields_push_failed(article_id, field_names, last_error or "Unknown push error")


def _run_score_task(
    storage: Storage,
    client: AIClient,
    syncer: NotionSync,
    article: Article,
    field_names: list[str],
    prompt_text: str,
    max_attempts: int,
    event_mode: bool = False,
) -> dict[str, Any]:
    existing_rows = storage.get_ai_results_for_article(article.id)
    if all(
        existing_rows.get(field_name)
        and existing_rows[field_name].get("status") == "completed"
        and existing_rows[field_name].get("push_status") != "completed"
        and existing_rows[field_name].get("value_text") is not None
        for field_name in field_names
    ):
        values = {field_name: str(existing_rows[field_name].get("value_text") or "") for field_name in field_names}
        if event_mode:
            storage.mark_ai_fields_push_deferred(article.id, list(values))
        else:
            _push_with_retry(storage, syncer, article.id, article.notion_page_id or "", values, max_attempts)
        return {"field_names": list(values.keys()), "status": "completed", "values": values}

    last_error = None
    for attempt in range(1, max_attempts + 1):
        storage.mark_ai_fields_processing(article.id, article.notion_page_id or "", field_names, "score", max_attempts=max_attempts)
        try:
            content, raw = client.chat_json(
                "你是一个严格执行 JSON 输出要求的中文文章评分助手。",
                prompt_text,
                disable_thinking=True,
            )
            if len(field_names) == 1:
                values = _parse_score_response(content, field_names[0])
            else:
                values = _parse_score_bundle_response(content, field_names)
            raw_response = json.dumps(raw, ensure_ascii=False)
            storage.mark_ai_fields_completed(article.id, values, raw_response)
            if event_mode:
                storage.mark_ai_fields_push_deferred(article.id, list(values))
            else:
                _push_with_retry(storage, syncer, article.id, article.notion_page_id or "", values, max_attempts)
            return {"field_names": list(values.keys()), "status": "completed", "values": values}
        except Exception as exc:
            last_error = str(exc)
            level = logging.INFO if client.is_rate_limit_error(exc) else logging.WARNING
            logger.log(
                level,
                "AI score task failed for article %s fields %s provider=%s attempt %s/%s: %s",
                article.id,
                field_names,
                client.provider_name,
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts and client.should_retry(exc, attempt):
                client.retry_sleep(attempt, exc)
            else:
                break
    storage.mark_ai_fields_failed(article.id, field_names, last_error or "Unknown error")
    return {"field_names": list(field_names), "status": "failed", "error": last_error}


def _run_meta_task(
    storage: Storage,
    client: AIClient,
    syncer: NotionSync,
    article: Article,
    prompt_text: str,
    max_attempts: int,
    event_mode: bool = False,
) -> dict[str, Any]:
    existing_rows = storage.get_ai_results_for_article(article.id)
    if all(
        existing_rows.get(field) and existing_rows[field].get("status") == "completed" and existing_rows[field].get("push_status") != "completed"
        for field in AI_META_FIELDS
    ):
        values = {field: str(existing_rows[field].get("value_text") or "") for field in AI_META_FIELDS}
        if event_mode:
            storage.mark_ai_fields_push_deferred(article.id, list(values))
        else:
            _push_with_retry(storage, syncer, article.id, article.notion_page_id or "", values, max_attempts)
        return {"field_names": list(values.keys()), "status": "completed", "values": values}

    last_error = None
    for attempt in range(1, max_attempts + 1):
        storage.mark_ai_fields_processing(article.id, article.notion_page_id or "", AI_META_FIELDS, "meta", max_attempts=max_attempts)
        try:
            content, raw = client.chat_json(
                "你是一个严格执行 JSON 输出要求的中文文章信息提炼助手。",
                _meta_user_prompt(prompt_text, article),
            )
            values = _parse_meta_response(content)
            raw_response = json.dumps(raw, ensure_ascii=False)
            storage.mark_ai_fields_completed(article.id, values, raw_response)
            if event_mode:
                storage.mark_ai_fields_push_deferred(article.id, list(values))
            else:
                _push_with_retry(storage, syncer, article.id, article.notion_page_id or "", values, max_attempts)
            return {"field_names": list(values.keys()), "status": "completed", "values": values}
        except Exception as exc:
            last_error = str(exc)
            level = logging.INFO if client.is_rate_limit_error(exc) else logging.WARNING
            logger.log(
                level,
                "AI meta task failed for article %s provider=%s attempt %s/%s: %s",
                article.id,
                client.provider_name,
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts and client.should_retry(exc, attempt):
                client.retry_sleep(attempt, exc)
            else:
                break
    storage.mark_ai_fields_failed(article.id, AI_META_FIELDS, last_error or "Unknown error")
    return {"field_names": list(AI_META_FIELDS), "status": "failed", "error": last_error}


def _build_pending_tasks(storage: Storage, article: Article, max_attempts: int) -> list[TaskSpec]:
    rows = storage.get_ai_results_for_article(article.id)
    queue_row = storage.get_ai_queue_rows([article.id]).get(article.id, {})
    event_phase = queue_row.get("phase") if queue_row.get("mode") == "event" else None
    prompt_cfg = storage.get_ai_prompt_config()
    score_prompts = prompt_cfg["score_prompts"]
    combined_prompt = str(prompt_cfg["combined_prompt"] or "").strip()
    tasks: list[TaskSpec] = []

    for field_name in ([] if event_phase == "meta" else SCORING_PROMPT_KEYS):
        prompt_text = str(score_prompts.get(field_name, "") or "").strip()
        if not prompt_text:
            continue
        row = rows.get(field_name)
        if row is None or (row["status"] not in {"completed", "skipped"} and row["attempt_count"] < max_attempts) or (
            event_phase is None and row["status"] == "completed" and row["push_status"] != "completed" and row["push_attempt_count"] < max_attempts
        ):
            storage.upsert_ai_result_stub(article.id, article.notion_page_id or "", field_name, "score", max_attempts=max_attempts)
            tasks.append(TaskSpec([field_name], "score", _score_user_prompt(field_name, prompt_text, article)))

    if combined_prompt and event_phase != "score":
        meta_rows = [rows.get(field_name) for field_name in AI_META_FIELDS]
        meta_needed = any(
            row is None or (row["status"] not in {"completed", "skipped"} and row["attempt_count"] < max_attempts) or (
                event_phase is None and row["status"] == "completed" and row["push_status"] != "completed" and row["push_attempt_count"] < max_attempts
            )
            for row in meta_rows
        )
        if meta_needed:
            for field_name in AI_META_FIELDS:
                storage.upsert_ai_result_stub(article.id, article.notion_page_id or "", field_name, "meta", max_attempts=max_attempts)
            tasks.append(TaskSpec(list(AI_META_FIELDS), "meta", combined_prompt))
    return tasks


def _advance_event_after_scores(storage: Storage, article: Article, margin: int) -> str:
    queue = storage.get_ai_queue_rows([article.id]).get(article.id, {})
    event_id = queue.get("event_id")
    if not event_id:
        return "blocked"
    decision = storage.decide_event_candidate(event_id, article.id, margin=margin)
    outcome = decision.get("decision", "blocked")
    if outcome == "loser":
        storage.mark_event_ai_queue_terminal(article.id, "loser")
    elif outcome in {"initial_winner", "winner", "replacement_pending"}:
        operation_id = decision.get("operation_id") or queue.get("operation_id") or f"meta:{event_id}:{article.id}"
        storage.advance_event_ai_queue(article.id, "meta", operation_id)
    return outcome


def _apply_event_representative(storage: Storage, syncer: NotionSync, article: Article, margin: int) -> bool:
    queue = storage.get_ai_queue_rows([article.id]).get(article.id, {})
    event_id = queue.get("event_id")
    event = storage.get_event(event_id) if event_id else None
    if not event:
        return False
    decision = storage.decide_event_candidate(event_id, article.id, margin=margin)
    if decision.get("decision") not in {"winner", "initial_winner", "replacement_pending"}:
        return False
    rows = storage.get_ai_results_for_article(article.id)
    if not all(rows.get(name) and rows[name]["status"] == "completed" for name in AI_ALL_FIELDS):
        return False
    values = {name: str(rows[name].get("value_text") or "") for name in AI_ALL_FIELDS}
    members = storage.list_event_members(event_id)
    result = syncer.apply_representative(
        event.get("notion_page_id") or article.notion_page_id or "",
        article,
        members,
        values,
        winner_id=article.id,
    )
    if not result.get("success"):
        logger.warning("Atomic event representative apply failed for article %s: %s", article.id, result.get("error"))
        return False
    if decision.get("decision") == "replacement_pending":
        committed = storage.set_event_winner(
            event_id,
            article.id,
            score_total=decision["score_total"],
            score_count=decision["score_count"],
            replacement=True,
            expected_old_winner=decision["expected_old_winner"],
            operation_id=decision["operation_id"],
        )
        if not committed:
            return False
    storage.mark_ai_fields_pushed(article.id, AI_ALL_FIELDS)
    storage.mark_event_ai_queue_terminal(article.id, "applied")
    return True


def enrich_articles_with_ai(
    storage: Storage,
    config: Config,
    api_key: str,
    database_id: str,
    article_ids: list[int] | None = None,
) -> dict[str, Any]:
    client = build_ai_client(config)
    missing_reason = client.missing_credentials_reason()
    if missing_reason:
        return {"success": True, "skipped": True, "reason": missing_reason}

    prompt_cfg = storage.get_ai_prompt_config()
    if not any((prompt_cfg["score_prompts"] or {}).values()) and not str(prompt_cfg["combined_prompt"] or "").strip():
        return {"success": True, "skipped": True, "reason": "No AI prompts configured"}

    syncer = NotionSync(api_key, database_id)
    max_attempts = client.max_attempts
    processed_articles = 0
    completed_fields = 0
    failed_fields = 0
    skipped_articles = 0
    score_futures = []
    meta_futures = []
    event_score_articles: dict[int, Article] = {}
    event_meta_articles: dict[int, Article] = {}

    if article_ids is not None:
        candidate_articles = []
        for article_id in article_ids:
            article = storage.get_article(article_id)
            if article is not None:
                candidate_articles.append(article)
    else:
        candidate_articles = storage.list_recent_synced_articles(
            limit=max(config.ai_enrichment_batch_size * 5, config.ai_enrichment_batch_size)
        )

    score_workers = 1
    meta_workers = 1
    if client.provider_name != "glm":
        score_workers = client.max_workers
        meta_workers = client.max_workers

    with ThreadPoolExecutor(max_workers=score_workers) as score_executor, ThreadPoolExecutor(max_workers=meta_workers) as meta_executor:
        for article in candidate_articles:
            if not article.notion_page_id:
                continue
            eligible, skip_reason = _article_content_eligibility(article)
            if not eligible:
                storage.mark_ai_fields_skipped(
                    article.id,
                    article.notion_page_id or "",
                    AI_ALL_FIELDS,
                    "gate",
                    skip_reason,
                    max_attempts=max_attempts,
                )
                skipped_articles += 1
                logger.info("Skipping AI enrichment for article %s (%s): %s", article.id, article.title, skip_reason)
                continue
            queue_row = storage.get_ai_queue_rows([article.id]).get(article.id, {})
            event_mode = queue_row.get("mode") == "event"
            if event_mode and queue_row.get("phase") == "score":
                event_score_articles[article.id] = article
            elif event_mode and queue_row.get("phase") == "meta":
                event_meta_articles[article.id] = article
            tasks = _build_pending_tasks(storage, article, max_attempts)
            if not tasks:
                continue
            processed_articles += 1
            for task in tasks:
                if task.request_group == "score":
                    score_futures.append(score_executor.submit(_run_score_task, storage, client, syncer, article, task.field_names, task.prompt_text, max_attempts, event_mode))
                else:
                    meta_futures.append(meta_executor.submit(_run_meta_task, storage, client, syncer, article, task.prompt_text, max_attempts, event_mode))
            if article_ids is None and processed_articles >= config.ai_enrichment_batch_size:
                break

        futures = score_futures + meta_futures
        if not futures and not event_score_articles and not event_meta_articles:
            return {
                "success": True,
                "provider": client.provider_name,
                "processed_articles": 0,
                "completed_fields": 0,
                "failed_fields": 0,
                "skipped_articles": skipped_articles,
            }

        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                logger.exception("Unexpected AI enrichment task crash: %s", exc)
                failed_fields += 1
                continue
            if result.get("status") == "completed":
                completed_fields += len(result.get("field_names", []))
            else:
                failed_fields += len(result.get("field_names", []))

        margin = int(getattr(config, "event_winner_margin_total", 2))
        for article in event_score_articles.values():
            _advance_event_after_scores(storage, article, margin)
        for article in event_meta_articles.values():
            _apply_event_representative(storage, syncer, article, margin)

    return {
        "success": True,
        "provider": client.provider_name,
        "processed_articles": processed_articles,
        "completed_fields": completed_fields,
        "failed_fields": failed_fields,
        "skipped_articles": skipped_articles,
    }
