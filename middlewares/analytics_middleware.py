import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, Message, CallbackQuery

from config import settings
from services.analytics import Analytics
from services.i18n import current_lang_var, resolve_user_lang

logger = logging.getLogger(__name__)

# Context variable allowing deep service calls (LLMRouter, TranscriptProvider) to know the active user_id
current_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_user_id", default=None
)


class AnalyticsMiddleware(BaseMiddleware):
    """
    Outer middleware registered on `dp.update` so both Messages and CallbackQueries
    are observed, `data["analytics"]` and `data["lang"]` are injected,
    `current_user_id_var` and `current_lang_var` are set for the whole request lifecycle,
    and a lightweight fire-and-forget 'update' event is recorded.
    """

    def __init__(self, analytics: Analytics) -> None:
        super().__init__()
        self.analytics = analytics
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_log(
        self, user_id: int | None, event_type: str, **props: Any
    ) -> None:
        try:
            task = asyncio.create_task(
                self.analytics.log_event(user_id, event_type, **props)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except Exception as exc:
            logger.warning("Failed to schedule analytics task: %s", exc)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["analytics"] = self.analytics

        user_id: int | None = None
        tg_lang_code: str | None = None
        update_kind = "other"
        media_kind = "other"

        msg: Message | None = None
        cb: CallbackQuery | None = None

        if isinstance(event, Update):
            msg = event.message or event.edited_message
            cb = event.callback_query
        elif isinstance(event, Message):
            msg = event
        elif isinstance(event, CallbackQuery):
            cb = event

        if msg is not None:
            update_kind = "message"
            if msg.from_user:
                user_id = msg.from_user.id
                tg_lang_code = msg.from_user.language_code
            if msg.video or (
                msg.document
                and msg.document.mime_type
                and msg.document.mime_type.startswith("video/")
            ):
                media_kind = "video"
            elif msg.audio or msg.voice:
                media_kind = "audio"
            elif msg.text or msg.caption:
                media_kind = "text"
        elif cb is not None:
            update_kind = "callback_query"
            if cb.from_user:
                user_id = cb.from_user.id
                tg_lang_code = cb.from_user.language_code
            media_kind = "button"

        stored_lang = await self.analytics.get_user_lang(user_id)
        resolved_lang = resolve_user_lang(
            telegram_lang_code=tg_lang_code,
            stored_lang=stored_lang,
            default_lang=settings.DEFAULT_LANG,
        )
        data["lang"] = resolved_lang

        uid_token = current_user_id_var.set(user_id)
        lang_token = current_lang_var.set(resolved_lang)
        try:
            self._spawn_log(
                user_id, "update", kind=update_kind, media=media_kind
            )
            return await handler(event, data)
        finally:
            current_lang_var.reset(lang_token)
            current_user_id_var.reset(uid_token)
