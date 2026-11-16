import asyncio
import contextlib
import html
import logging
import time
from typing import Literal

from aiogram import Router, F
from aiogram.types import CallbackQuery, LinkPreviewOptions

from config import settings
from handlers.video_handler import (
    ActionStatusAnimator,
    VideoActionCallback,
)
from services.analytics import Analytics
from services.i18n import t
from services.job_manager import JobManager, JobRejectedError
from services.link_server import LinkServer
from services.llm_router import LLMCallMeta, LLMRouter
from services.media_links import MediaLinkService
from services.subtitle_service import SubtitleService
from services.transcript_provider import TranscriptFetchError

logger = logging.getLogger(__name__)
link_router = Router(name="direct_streaming_link_router")

ACTION_KEY_PAIRS: dict[str, tuple[str, str]] = {
    "link_sub": ("link_label_sub", "link_pipe_sub"),
    "link_orig": ("link_label_orig", "link_pipe_orig"),
    "link_audio": ("link_label_audio", "link_pipe_audio"),
}


def _format_ttl_localized(ttl_sec: int) -> str:
    hours, rem = divmod(max(60, int(ttl_sec)), 3600)
    mins = rem // 60
    if hours > 0 and mins > 0:
        return t("ttl_hours_mins", hours=hours, mins=mins)
    if hours > 0:
        return t("ttl_hours_only", hours=hours)
    return t("ttl_mins_only", mins=mins)


@link_router.callback_query(
    VideoActionCallback.filter(
        F.action.in_({"link_sub", "link_orig", "link_audio"})
    )
)
async def cb_generate_direct_link(
    callback: CallbackQuery,
    callback_data: VideoActionCallback,
    subtitle_service: SubtitleService,
    media_link_service: MediaLinkService,
    link_server: LinkServer,
    analytics: Analytics,
    llm_router: LLMRouter,
    job_manager: JobManager,
) -> None:
    """Handle direct streaming link generation for link_sub, link_orig, and link_audio."""
    action_name = callback_data.action
    if not settings.LINK_PUBLIC_BASE_URL or not link_server.is_enabled:
        await callback.answer(
            t("link_not_configured_alert"),
            show_alert=True,
        )
        return

    user_id = callback.from_user.id if callback.from_user else None
    label_key, pipe_key = ACTION_KEY_PAIRS.get(
        action_name, ACTION_KEY_PAIRS["link_orig"]
    )
    label_text = t(label_key)
    pipeline_desc = t(pipe_key)

    await callback.answer(t("cb_preparing_item", item=label_text))
    if not callback.message:
        return

    meta = await subtitle_service.get_video_metadata(callback_data.video_id)

    try:
        media_link_service.check_link_duration(
            meta.duration_seconds, user_id=user_id
        )
    except JobRejectedError as exc:
        await callback.message.reply(f"⚠️ <b>{html.escape(exc.user_message_fa)}</b>")
        return

    status_msg = await callback.message.reply(
        t("msg_preparing_item", item=html.escape(label_text))
    )
    animator = ActionStatusAnimator(
        status_msg=status_msg,
        title=meta.title,
        pipeline_header=pipeline_desc,
    )
    animator.start_heartbeat()
    t0 = time.monotonic()

    async def _on_switch(m: LLMCallMeta) -> None:
        if m.provider == "openrouter":
            await animator.update(
                animator.percent,
                t("sub_openrouter_switch"),
            )

    async def _on_queue(pos: int) -> None:
        await animator.update(5, t("queue_position", pos=pos))

    async def _on_shutdown(msg_text: str) -> None:
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"⚠️ <b>{html.escape(msg_text)}</b>")

    hard_timeout = (
        float(settings.LINK_TRANSLATION_TIMEOUT_SEC) + 180.0
        if action_name == "link_sub"
        else 240.0
    )
    kind_literal: Literal["link_sub", "link_orig", "link_audio"] = (
        "link_sub"
        if action_name == "link_sub"
        else ("link_audio" if action_name == "link_audio" else "link_orig")
    )

    try:
        async with job_manager.run_heavy_job(
            user_id=user_id,
            kind="video",
            on_queue_position=_on_queue,
            on_shutdown_notify=_on_shutdown,
            require_disk_check=True,
        ):
            async with llm_router.request_context(
                user_id=user_id, on_switch=_on_switch
            ) as scope:
                logger.info(
                    "Direct link action '%s' started for video=%s",
                    action_name,
                    callback_data.video_id,
                )
                result = await asyncio.wait_for(
                    media_link_service.create_link(
                        video_id=callback_data.video_id,
                        kind=kind_literal,
                        progress_cb=animator.update,
                    ),
                    timeout=hard_timeout,
                )

                await animator.stop_heartbeat()
                size_mb = round(result.size_bytes / (1024 * 1024), 1)
                ttl_str = _format_ttl_localized(result.ttl_sec)

                lines: list[str] = [
                    t(
                        "link_ready_message",
                        label=html.escape(label_text),
                        title=html.escape(result.title),
                        channel=html.escape(result.channel),
                        size_mb=size_mb,
                        ttl=html.escape(ttl_str),
                        url=html.escape(result.url),
                    )
                ]

                if result.approximate_timing:
                    lines.append(f"\n{t('approximate_timing_notice')}")
                if scope.openrouter_used and scope.openrouter_model:
                    lines.append(
                        t("openrouter_footer", model=html.escape(scope.openrouter_model))
                    )

                await status_msg.edit_text(
                    "\n".join(lines),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )

                dur = round(time.monotonic() - t0, 3)
                logger.info(
                    "Direct link action '%s' finished in %.2fs",
                    action_name,
                    dur,
                )
                await analytics.log_event(
                    user_id,
                    "link_created",
                    kind=action_name,
                    seconds=dur,
                )

    except JobRejectedError as exc:
        await animator.stop_heartbeat()
        await status_msg.edit_text(f"⚠️ <b>{html.escape(exc.user_message_fa)}</b>")
    except TranscriptFetchError as exc:
        await animator.stop_heartbeat()
        await analytics.log_event(
            user_id, "action_failed", action=action_name, error_type=exc.error_kind
        )
        await status_msg.edit_text(f"❌ <b>{html.escape(exc.user_message_fa)}</b>")
    except asyncio.TimeoutError:
        await animator.stop_heartbeat()
        dur = round(time.monotonic() - t0, 3)
        logger.warning("Direct link action '%s' timed out after %.2fs", action_name, dur)
        await analytics.log_event(
            user_id, "action_failed", action=action_name, error_type="TimeoutError"
        )
        await status_msg.edit_text(t("err_link_timeout"))
    except Exception as exc:
        await animator.stop_heartbeat()
        logger.exception("Error in direct link action '%s': %s", action_name, exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action=action_name,
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(t("err_link_generic"))
