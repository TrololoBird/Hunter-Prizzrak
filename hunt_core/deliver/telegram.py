from __future__ import annotations


import asyncio
import hashlib
import html
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeVar, cast

import aiohttp
import structlog

from hunt_core.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# Legacy Telegram sender retained for callers that still depend on this module.
# New runtime delivery code lives under bot/telegram/.
try:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.types import BufferedInputFile

    try:
        from aiogram.exceptions import TelegramAPIError as _AiogramAPIError
        from aiogram.exceptions import TelegramRetryAfter as _TelegramRetryAfter

        AiogramAPIError: Any = _AiogramAPIError
        TelegramRetryAfter: Any = _TelegramRetryAfter
    except ImportError:
        from aiogram import exceptions as aiogram_exceptions

        AiogramAPIError = getattr(aiogram_exceptions, "TelegramAPIError", Exception)
        TelegramRetryAfter = getattr(aiogram_exceptions, "TelegramRetryAfter", None)
    _HAS_AIogram = True
except ImportError:
    _HAS_AIogram = False
    BufferedInputFile = None  # type: ignore[misc, assignment]
    TelegramRetryAfter = None
    AiogramAPIError = Exception

# tenacity for retries
try:
    from tenacity import (
        before_sleep_log,
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )

    HAS_TENACITY = True
except ImportError:
    HAS_TENACITY = False


LOG = structlog.get_logger("hunt_core.deliver.telegram")
RETRY_LOG = logging.getLogger("hunt_core.deliver.telegram")
P = ParamSpec("P")
R = TypeVar("R")
NETWORK_RETRIES = 3
RETRY_DELAY_SECONDS = 1.5
TELEGRAM_DUPLICATE_WINDOW_SECONDS = 180
TELEGRAM_TEXT_LIMIT = 4000
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_LOG_PREVIEW_LIMIT = 500
TELEGRAM_TAGS = re.compile(r"</?(?:b|i|code|pre|a)[^>]*>", flags=re.IGNORECASE)
TELEGRAM_CHUNK_LIMIT = 3900

# Fallback retry decorator for when tenacity is not installed
def _simple_retry(
    max_attempts: int = 3, exceptions: tuple[type[Exception], ...] = (Exception,)
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Simple retry decorator as fallback when tenacity is not available."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        wait_time = RETRY_DELAY_SECONDS * (2**attempt)  # Exponential backoff
                        LOG.debug(
                            "retry %s/%s after %.1fs: %s",
                            attempt + 1,
                            max_attempts,
                            wait_time,
                            exc,
                        )
                        await asyncio.sleep(wait_time)
            raise last_exc or RuntimeError("Retry failed")

        return wrapper

    return decorator


def _buffered_input_file_class() -> Any:
    if BufferedInputFile is None:
        msg = "BufferedInputFile is unavailable"
        raise RuntimeError(msg)
    return BufferedInputFile


def _extract_retry_after_seconds(description: str) -> int | None:
    match = re.search(r"retry after\s+(\d+)", str(description or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _telegram_rate_limit_wait(exc: BaseException) -> int | None:
    """Seconds to wait when Telegram returns flood-control (RetryAfter / 429)."""
    if TelegramRetryAfter is not None and isinstance(exc, TelegramRetryAfter):
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            try:
                value = int(retry_after)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            value = int(retry_after)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    parsed = _extract_retry_after_seconds(str(exc))
    if parsed:
        return parsed
    name = exc.__class__.__name__.lower()
    if "retryafter" in name or "too many requests" in str(exc).lower():
        return 30
    return None


def _telegram_retryable(exc: BaseException) -> bool:
    if _telegram_rate_limit_wait(exc) is not None:
        return False
    return isinstance(exc, Exception)


def _telegram_retry() -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    if HAS_TENACITY:
        return cast(
            "Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]",
            retry(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=10),
                retry=retry_if_exception(_telegram_retryable),
                before_sleep=before_sleep_log(RETRY_LOG, logging.INFO),
                reraise=True,
            ),
        )
    return _simple_retry(3, (Exception,))


class MessageBroadcaster(Protocol):
    async def preflight_check(self) -> None: ...
    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult: ...
    async def edit_html(self, message_id: int, text: str) -> None: ...
    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str
    message_id: int | None = None
    reason: str | None = None


class DisabledBroadcaster:
    """No-op broadcaster for runtime modes with external delivery disabled."""

    async def preflight_check(self) -> None:
        msg = "notifier provider is disabled; signal delivery is local/log only"
        raise RuntimeError(msg)

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        del text, reply_to_message_id
        return DeliveryResult(status="logged", reason="notifier_disabled")

    async def edit_html(self, message_id: int, text: str) -> None:
        del message_id, text
        return

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        return None

    async def close(self) -> None:
        return None


class TelegramBroadcaster:
    duplicate_window_seconds = TELEGRAM_DUPLICATE_WINDOW_SECONDS
    min_send_interval_seconds = 1.25

    def __init__(self, token: str, target_chat_id: str, proxy_url: str | None = None) -> None:
        if not _HAS_AIogram:
            msg = "aiogram not installed. Run: pip install aiogram>=3.27.0"
            raise RuntimeError(msg)

        self.token = token
        self.target_chat_id = target_chat_id
        session = AiohttpSession(proxy=proxy_url)
        self.bot = Bot(token=token, session=session)
        self._send_lock = asyncio.Lock()
        self._failure_count = 0
        self._circuit_state = "closed"
        self._circuit_reset_time: datetime | None = None
        self._recent_message_hashes: dict[str, datetime] = {}
        self._send_buffer: deque[str] = deque(maxlen=50)
        self._rate_limit_until: datetime | None = None
        self._last_send_monotonic: float = 0.0

    async def preflight_check(self) -> None:
        """Verify bot token and chat access."""
        try:
            bot_info = await self.bot.get_me()
            LOG.info("telegram bot info", username=bot_info.username, id=bot_info.id)

            chat = await self.bot.get_chat(self.target_chat_id)
            LOG.info("telegram chat access confirmed", chat_id=chat.id, type=chat.type)
        except Exception as exc:
            msg = f"telegram preflight failed: {exc}"
            raise RuntimeError(msg) from exc

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None, no_split: bool = False
    ) -> DeliveryResult:
        async with self._send_lock:
            await self._respect_rate_limit()
            if self._circuit_state == "open":
                if (
                    self._circuit_reset_time is not None
                    and datetime.now(UTC) < self._circuit_reset_time
                ):
                    self._send_buffer.append(text)
                    LOG.debug(
                        "telegram circuit breaker open; buffering message (%s buffered)",
                        len(self._send_buffer),
                    )
                    return DeliveryResult(status="buffered_circuit_open", reason="circuit_open")
                self._circuit_state = "half_open"
            self._prune_recent_hashes()
            message_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if message_hash in self._recent_message_hashes:
                LOG.debug("suppressing duplicate telegram message within dedupe window")
                return DeliveryResult(
                    status="suppressed_duplicate", reason="duplicate_within_window"
                )
            try:
                if no_split and len(text) > TELEGRAM_CHUNK_LIMIT:
                    text = _balance_html_fragment(text[: TELEGRAM_CHUNK_LIMIT - 12].rstrip()) + "\n…"
                parts = [text] if no_split else _split_telegram_text(text)
                sent_message_id: int | None = None
                for idx, part in enumerate(parts):
                    if len(parts) > 1:
                        marker = f"<i>📄 {idx + 1}/{len(parts)}</i>\n\n"
                        part = marker + part
                    part_hash = (
                        message_hash if len(parts) == 1 else f"{message_hash}:{idx}"
                    )
                    sent_message_id = await self._send_immediate(
                        part,
                        message_hash=part_hash,
                        reply_to_message_id=(
                            reply_to_message_id if idx == 0 else None
                        ),
                    )
            except DEFENSIVE_EXC as exc:
                return DeliveryResult(status="failed", reason=f"{exc.__class__.__name__}: {exc}")
            while self._send_buffer:
                buffered = self._send_buffer.popleft()
                buffered_hash = hashlib.sha256(buffered.encode("utf-8")).hexdigest()
                if buffered_hash in self._recent_message_hashes:
                    continue
                try:
                    await self._send_immediate(
                        buffered, message_hash=buffered_hash, reply_to_message_id=None
                    )
                except DEFENSIVE_EXC as exc:
                    LOG.debug("telegram buffered message retry failed", error=str(exc))
                    self._send_buffer.appendleft(buffered)
                    break
            return DeliveryResult(status="sent", message_id=sent_message_id)

    async def edit_html(self, message_id: int, text: str) -> None:
        async with self._send_lock:
            rate_retries = 0
            max_rate_retries = 4
            while True:
                await self._respect_rate_limit()
                if self._circuit_state == "open":
                    if (
                        self._circuit_reset_time is not None
                        and datetime.now(UTC) < self._circuit_reset_time
                    ):
                        LOG.debug(
                            "telegram circuit breaker open; skipping edit for message_id=%s",
                            message_id,
                        )
                        return
                    self._circuit_state = "half_open"
                try:
                    await self._edit_immediate(message_id, text)
                except DEFENSIVE_EXC as exc:
                    self._record_send_failure(exc)
                    raise
                except Exception as exc:
                    wait = _telegram_rate_limit_wait(exc)
                    if wait is not None and rate_retries < max_rate_retries:
                        rate_retries += 1
                        self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=wait)
                        LOG.warning(
                            "telegram edit flood control; waiting %ss | attempt=%d/%d id=%s",
                            wait + 1,
                            rate_retries,
                            max_rate_retries,
                            message_id,
                        )
                        await asyncio.sleep(wait + 1)
                        continue
                    if wait is not None:
                        LOG.warning(
                            "telegram edit flood control exhausted; skipping edit | message_id=%s",
                            message_id,
                        )
                        return
                    raise
                else:
                    self._failure_count = 0
                    self._circuit_state = "closed"
                    self._circuit_reset_time = None
                    return

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Send photo using aiogram Bot with BufferedInputFile."""
        async with self._send_lock:
            await self._respect_rate_limit()
            html_caption, plain_caption = self._prepare_captions(caption)

            try:
                BufferedInputFile = _buffered_input_file_class()
                photo_file = BufferedInputFile(photo_bytes, filename="chart.png")
                await self._respect_min_send_interval()
                await self.bot.send_photo(
                    chat_id=self.target_chat_id,
                    photo=photo_file,
                    caption=html_caption,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_message_id,
                )
                self._mark_send_timestamp()
            except DEFENSIVE_EXC as exc:
                error_str = str(exc).lower()
                # Try plain text fallback if HTML parsing failed
                if "parse" in error_str or "html" in error_str or "caption" in error_str:
                    fallback_caption = self._plain_text_fallback(caption, exc) or plain_caption
                    try:
                        BufferedInputFile = _buffered_input_file_class()
                        photo_file = BufferedInputFile(photo_bytes, filename="chart.png")
                        await self._respect_min_send_interval()
                        await self.bot.send_photo(
                            chat_id=self.target_chat_id,
                            photo=photo_file,
                            caption=fallback_caption,
                            reply_to_message_id=reply_to_message_id,
                        )
                        self._mark_send_timestamp()
                    except DEFENSIVE_EXC:
                        LOG.exception("telegram photo send failed (fallback)")
                        raise
                else:
                    LOG.exception("telegram photo send failed")
                    raise

    def _prune_recent_hashes(self) -> None:
        now = datetime.now(UTC)
        self._recent_message_hashes = {
            key: sent_at
            for key, sent_at in self._recent_message_hashes.items()
            if (now - sent_at).total_seconds() < type(self).duplicate_window_seconds
        }

    @_telegram_retry()
    async def _send_immediate(
        self,
        text: str,
        *,
        message_hash: str,
        reply_to_message_id: int | None,
    ) -> int | None:
        """Send message using aiogram Bot."""
        try:
            await self._respect_min_send_interval()
            result = await self.bot.send_message(
                chat_id=self.target_chat_id,
                text=text,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True,
            )
            self._record_send_success(message_hash)
            LOG.info("telegram message sent", chars=len(text), preview=_message_preview(text))
        except (*DEFENSIVE_EXC, AiogramAPIError) as exc:
            error_str = str(exc).lower()
            if (
                "too long" in error_str
                or "text is too long" in error_str
                or "parse" in error_str
                or "html" in error_str
                or "tag" in error_str
            ):
                plain_text = self._plain_text_fallback(text, exc)
                if plain_text:
                    try:
                        await self._respect_min_send_interval()
                        result = await self.bot.send_message(
                            chat_id=self.target_chat_id,
                            text=plain_text,
                            reply_to_message_id=reply_to_message_id,
                            disable_web_page_preview=True,
                        )
                        self._record_send_success(message_hash)
                        LOG.info("telegram message sent (plain text)", chars=len(plain_text))
                    except DEFENSIVE_EXC as fallback_exc:
                        self._record_send_failure(fallback_exc)
                        raise
                    else:
                        return result.message_id
            self._record_send_failure(exc)
            raise
        else:
            return result.message_id

    @_telegram_retry()
    async def _edit_immediate(self, message_id: int, text: str) -> None:
        """Edit message using aiogram Bot."""
        try:
            await self._respect_min_send_interval()
            await self.bot.edit_message_text(
                chat_id=self.target_chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except (*DEFENSIVE_EXC, AiogramAPIError) as exc:
            error_str = str(exc).lower()
            # Message not modified is OK
            if "not modified" in error_str or "message is not modified" in error_str:
                return
            # Try plain text fallback
            if "parse" in error_str or "html" in error_str:
                plain_text = self._plain_text_fallback(text, exc)
                if plain_text:
                    try:
                        await self._respect_min_send_interval()
                        await self.bot.edit_message_text(
                            chat_id=self.target_chat_id,
                            message_id=message_id,
                            text=plain_text,
                            disable_web_page_preview=True,
                        )
                    except DEFENSIVE_EXC as fallback_exc:
                        if "not modified" in str(fallback_exc).lower():
                            return
                        raise
                    else:
                        return
            raise

    def _mark_send_timestamp(self) -> None:
        self._last_send_monotonic = time.monotonic()

    def _build_payload(
        self,
        text: str,
        *,
        parse_mode: str | None,
        reply_to_message_id: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self.target_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        return payload

    def _record_send_success(self, message_hash: str) -> None:
        self._failure_count = 0
        self._circuit_state = "closed"
        self._circuit_reset_time = None
        self._rate_limit_until = None
        self._recent_message_hashes[message_hash] = datetime.now(UTC)
        self._mark_send_timestamp()

    def _record_send_failure(self, exc: BaseException) -> None:
        retry_after = _telegram_rate_limit_wait(exc)
        if retry_after:
            self._rate_limit_until = datetime.now(UTC) + timedelta(seconds=retry_after)
            LOG.warning("telegram rate limited; pausing sends", seconds=retry_after)
            return

        self._failure_count += 1
        LOG.error("telegram send failed", attempt=f"{self._failure_count}/5", error=str(exc))

        if self._circuit_state == "half_open" or self._failure_count >= 5:
            self._circuit_state = "open"
            self._circuit_reset_time = datetime.now(UTC) + timedelta(minutes=5)
            LOG.critical("telegram circuit breaker opened for 5 minutes")

    async def _respect_rate_limit(self) -> None:
        if self._rate_limit_until is None:
            return
        remaining = (self._rate_limit_until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            self._rate_limit_until = None
            return
        LOG.info("telegram send throttled by retry_after | sleep=%.1fs", remaining)
        await asyncio.sleep(remaining)
        self._rate_limit_until = None

    async def _respect_min_send_interval(self) -> None:
        interval = max(0.0, float(type(self).min_send_interval_seconds))
        if interval <= 0.0 or self._last_send_monotonic <= 0.0:
            return
        elapsed = time.monotonic() - self._last_send_monotonic
        delay = interval - elapsed
        if delay <= 0.0:
            return
        LOG.debug("telegram send paced", sleep_seconds=round(delay, 3))
        await asyncio.sleep(delay)

    @staticmethod
    def _plain_text_fallback(text: str, exc: BaseException | None = None) -> str | None:
        """Convert HTML to plain text when Telegram rejects HTML parsing."""
        # Check if exception indicates recoverable HTML error
        if exc is not None:
            error_str = str(exc).lower()
            recoverable_fragments = (
                "can't parse entities",
                "unsupported start tag",
                "can't find end tag",
                "message is too long",
                "text is too long",
                "caption",
                "html",
            )
            if not any(fragment in error_str for fragment in recoverable_fragments):
                return None

        # Normalize HTML to plain text
        normalized = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        normalized = re.sub(r"</p\s*>", "\n\n", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"<[^>]+>", "", normalized)
        normalized = html.unescape(normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()

        if not normalized:
            return None
        if len(normalized) > TELEGRAM_TEXT_LIMIT:
            normalized = normalized[: TELEGRAM_TEXT_LIMIT - 1].rstrip() + "…"
        return normalized

    @staticmethod
    def _prepare_captions(text: str) -> tuple[str, str]:
        """Prepare HTML and plain text versions of caption."""
        html_caption = text.strip()
        if len(html_caption) <= TELEGRAM_CAPTION_LIMIT:
            # Generate plain fallback for safety
            plain_caption = (
                TelegramBroadcaster._plain_text_fallback(html_caption, None) or html_caption
            )
            if len(plain_caption) > TELEGRAM_CAPTION_LIMIT:
                plain_caption = plain_caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"
            return html_caption, plain_caption

        # For oversized captions, convert to plain text
        plain_caption = TelegramBroadcaster._plain_text_fallback(html_caption, None) or html_caption
        if len(plain_caption) > TELEGRAM_CAPTION_LIMIT:
            plain_caption = plain_caption[: TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"
        return plain_caption, plain_caption

    async def close(self) -> None:
        """Close aiogram bot session."""
        if not self.bot:
            return
        try:
            await self.bot.session.close()
        finally:
            self.bot = None  # type: ignore[assignment]
            LOG.info("telegram bot session closed")


# Telegram supports only a small set of inline tags (b/i/code/pre/u/s/a/…), all
# properly nested. When we truncate or hard-split HTML we can land in the middle
# of a <i>…</i> span; the resulting chunk has an unclosed tag and Telegram rejects
# it with "Can't find end tag corresponding to start tag ...". These helpers make
# every emitted chunk tag-balanced.
_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z]+)(?:\s[^>]*)?>")


def _strip_partial_tag_tail(fragment: str) -> str:
    """Drop a trailing ``<…`` that was cut before its closing ``>``."""
    lt = fragment.rfind("<")
    if lt != -1 and fragment.find(">", lt) == -1:
        return fragment[:lt].rstrip()
    return fragment


def _open_tag_stack(fragment: str) -> list[str]:
    """Tag names still open at the end of ``fragment`` (proper nesting assumed)."""
    stack: list[str] = []
    for closing, raw_name in _HTML_TAG_RE.findall(fragment):
        name = raw_name.lower()
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:  # tolerate minor mis-nesting
                stack.remove(name)
        else:
            stack.append(name)
    return stack


def _close_open_tags(fragment: str) -> tuple[str, list[str]]:
    """Strip a dangling partial tag, then return (balanced_fragment, tags_left_open)."""
    safe = _strip_partial_tag_tail(fragment)
    stack = _open_tag_stack(safe)
    balanced = safe + "".join(f"</{name}>" for name in reversed(stack))
    return balanced, stack


def _balance_html_fragment(fragment: str) -> str:
    """Remove a dangling partial tag and append closers for any still-open tags."""
    return _close_open_tags(fragment)[0]


def _rebalance_parts(parts: list[str]) -> list[str]:
    """Close tags left open at each chunk boundary and reopen them in the next chunk."""
    if len(parts) <= 1:
        return parts
    out: list[str] = []
    carry: list[str] = []
    for part in parts:
        balanced, carry = _close_open_tags("".join(f"<{name}>" for name in carry) + part)
        out.append(balanced)
    return out


def _split_telegram_text(text: str, *, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split HTML message into Telegram-safe chunks (paragraph-aware + hard split)."""
    if len(text) <= limit:
        return [text]

    def _hard_split(block: str) -> list[str]:
        if len(block) <= limit:
            return [block] if block else []
        out: list[str] = []
        start = 0
        while start < len(block):
            end = min(start + limit, len(block))
            if end < len(block):
                nl = block.rfind("\n", start, end)
                if nl > start + limit // 3:
                    end = nl
            piece = block[start:end].strip()
            if piece:
                out.append(piece)
            start = end if end > start else end + 1
        return out

    parts: list[str] = []
    chunk = ""
    for block in text.split("\n\n"):
        candidate = f"{chunk}\n\n{block}".strip() if chunk else block
        if len(candidate) <= limit:
            chunk = candidate
            continue
        if chunk:
            parts.extend(_hard_split(chunk))
            chunk = ""
        if len(block) <= limit:
            chunk = block
        else:
            parts.extend(_hard_split(block))
    if chunk:
        parts.extend(_hard_split(chunk))
    return _rebalance_parts(parts or _hard_split(text[:limit]))


def _message_preview(text: str, *, limit: int = TELEGRAM_LOG_PREVIEW_LIMIT) -> str:
    preview = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 1].rstrip() + "…"


def _html_to_plain_text(text: str) -> str:
    stripped = TELEGRAM_TAGS.sub("", text or "")
    stripped = re.sub(r"<[^>]+>", "", stripped)
    return html.unescape(stripped).strip()


class WebhookBroadcaster:
    def __init__(
        self,
        *,
        provider: str,
        webhook_url: str,
        username: str | None = None,
        bearer_token: str | None = None,
        include_html: bool = True,
    ) -> None:
        self.provider = provider
        self.webhook_url = webhook_url
        self.username = username
        self.bearer_token = bearer_token
        self.include_html = include_html
        self._session = aiohttp.ClientSession()

    async def preflight_check(self) -> None:
        if not self.webhook_url:
            msg = f"{self.provider} webhook_url is required"
            raise RuntimeError(msg)
        LOG.info("webhook broadcaster configured", provider=self.provider)

    async def send_html(
        self, text: str, *, reply_to_message_id: int | None = None
    ) -> DeliveryResult:
        del reply_to_message_id
        plain_text = _html_to_plain_text(text)
        payload = self._build_payload(text, plain_text)
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        try:
            async with self._session.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    LOG.error(
                        "webhook delivery failed | provider=%s status=%s body=%s",
                        self.provider,
                        response.status,
                        body[:200],
                    )
                    return DeliveryResult(status="failed", reason=f"http_{response.status}")
        except DEFENSIVE_EXC as exc:
            LOG.exception("webhook delivery failed | provider=%s", self.provider)
            return DeliveryResult(status="failed", reason=f"{exc.__class__.__name__}: {exc}")
        return DeliveryResult(status="sent")

    async def edit_html(self, message_id: int, text: str) -> None:
        del message_id, text
        LOG.debug("webhook broadcaster does not support edits | provider=%s", self.provider)

    async def send_photo(
        self,
        photo_bytes: bytes,
        caption: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        del photo_bytes
        await self.send_html(caption, reply_to_message_id=reply_to_message_id)

    async def close(self) -> None:
        await self._session.close()

    def _build_payload(self, html_text: str, plain_text: str) -> dict[str, Any]:
        if self.provider == "slack":
            return {"text": plain_text}
        if self.provider == "discord":
            payload: dict[str, Any] = {"content": plain_text}
            if self.username:
                payload["username"] = self.username
            return payload
        payload = {"text": plain_text}
        if self.include_html:
            payload["html"] = html_text
        if self.username:
            payload["username"] = self.username
        return payload


def build_message_broadcaster(settings: Any, proxy_url: str | None = None) -> MessageBroadcaster:
    provider = str(
        getattr(getattr(settings, "notifiers", None), "provider", "telegram") or "telegram"
    ).lower()
    if provider == "none":
        return DisabledBroadcaster()
    if provider == "telegram":
        token = str(getattr(settings, "tg_token", "") or "").strip()
        chat_id = str(getattr(settings, "target_chat_id", "") or "").strip()
        if not token or not chat_id:
            return DisabledBroadcaster()
        return TelegramBroadcaster(token, chat_id, proxy_url=proxy_url)

    provider_config = getattr(settings.notifiers, provider, None)
    if provider_config is None or not getattr(provider_config, "webhook_url", None):
        msg = f"notifier provider {provider!r} requires notifiers.{provider}.webhook_url"
        raise RuntimeError(msg)

    return WebhookBroadcaster(
        provider=provider,
        webhook_url=str(provider_config.webhook_url),
        username=getattr(provider_config, "username", None),
        bearer_token=getattr(provider_config, "bearer_token", None),
        include_html=bool(getattr(provider_config, "include_html", True)),
    )

# --- merged from deliver/telegram (formatters) — public re-exports ---

from hunt_core.deliver._followup import format_followup_telegram
from hunt_core.deliver._labels import (
    fmt_price,
    format_symbol_telegram,
    phase_badge,
    phase_human,
    rr_display,
    rr_emoji,
    trigger_human,
    veto_human,
)
from hunt_core.deliver._sections import (
    format_cross_exchange_section,
    format_pinned_deep_analysis,
)
from hunt_core.deliver.templates import format_squeeze_telegram as _format_squeeze_telegram


def squeeze_trade_direction(row: dict[str, Any]) -> str:
    """short | long for unified advisory cooldown on squeeze alerts."""
    sq = row.get("squeeze") or {}
    lifecycle = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    bias = str(lifecycle.get("recommended_bias") or "")
    if bias in {"short", "long"}:
        return bias
    dump_score = float(dump.get("dump_score") or 0)
    long_score = float(long_setup.get("long_score") or 0)
    if dump_score > long_score + 5:
        return "short"
    if long_score > dump_score + 5:
        return "long"
    try:
        oi_z = float(sq.get("oi_z") or 0)
        if oi_z < -0.8:
            return "short"
        if oi_z > 0.8:
            return "long"
    except (TypeError, ValueError):
        pass
    return "short" if dump_score >= long_score else "long"


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    return _format_squeeze_telegram(row)


def format_setup_lines(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
) -> list[str]:
    from hunt_core.runtime.cycle._cycle_format import _format_setup_lines

    return _format_setup_lines(
        row,
        setup,
        direction=direction,
        tf=tf,
        pos=pos,
        price=price,
    )


from hunt_core.deliver._context_lines import (
    structured_thesis_lines as _structured_thesis_lines,
)



def _format_structured_thesis(
    setup: dict[str, Any],
    *,
    direction: str,
    lc_phase: str,
    confirm_reasons: list[str],
    entry_mid: float,
) -> tuple[list[str], str]:
    return _structured_thesis_lines(
        setup,
        direction=direction,
        lc_phase=lc_phase,
        confirm_reasons=confirm_reasons,
        entry_mid_px=entry_mid,
    )






def split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    return _split_telegram_text(text, limit=limit)


async def send_telegram_chunks(
    broadcaster: TelegramBroadcaster,
    text: str,
    *,
    log_key: str,
    log: Any,
) -> bool:
    ok = True
    for idx, part in enumerate(split_telegram(text)):
        result = await broadcaster.send_html(part)
        if result.status != "sent":
            log.warning(
                f"{log_key}_failed",
                part=idx + 1,
                status=result.status,
                reason=result.reason,
            )
            ok = False
        else:
            log.info(f"{log_key}_sent", part=idx + 1, message_id=result.message_id)
    return ok


__all__ = (
    "DeliveryResult",
    "MessageBroadcaster",
    "TelegramBroadcaster",
    "WebhookBroadcaster",
    "build_message_broadcaster",
    "fmt_price",
    "format_cross_exchange_section",
    "format_followup_telegram",
    "format_pinned_deep_analysis",
    "format_setup_lines",
    "format_squeeze_telegram",
    "format_symbol_telegram",
    "phase_badge",
    "phase_human",
    "rr_display",
    "rr_emoji",
    "send_telegram_chunks",
    "split_telegram",
    "squeeze_trade_direction",
    "trigger_human",
    "veto_human",
    "_split_telegram_text",
)
