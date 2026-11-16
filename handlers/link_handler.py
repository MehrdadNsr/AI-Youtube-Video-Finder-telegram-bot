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
    APPROXIMATE_TIMING_LINE_FA,
    OPENROUTER_FOOTER_TEMPLATE,
    VideoActionCallback,
)
from services.analytics import Analytics
from services.job_manager import JobManager, JobRejectedError
from services.link_server import LinkServer
from services.llm_router import LLMCallMeta, LLMRouter
from services.media_links import MediaLinkService
from services.subtitle_service import SubtitleService
from services.transcript_provider import TranscriptFetchError

logger = logging.getLogger(__name__)
link_router = Router(name="direct_streaming_link_router")

ACTION_LABELS_FA: dict[str, tuple[str, str]] = {
    "link_sub": (
        "لینک مستقیم ویدیو با سافت‌ساب فارسی",
        "ترجمه زیرنویس -> دانلود ویدیو -> الصاق سافت‌ساب -> ساخت لینک",
    ),
    "link_orig": (
        "لینک مستقیم ویدیوی اصلی (بدون زیرنویس)",
        "دریافت استریم 720p از یوتیوب -> آماده‌سازی لینک مستقیم",
    ),
    "link_audio": (
        "لینک مستقیم فایل صوتی (M4A)",
        "دریافت صدای باکیفیت از یوتیوب -> آماده‌سازی لینک مستقیم",
    ),
}


def _format_ttl_persian(ttl_sec: int) -> str:
    hours, rem = divmod(max(60, int(ttl_sec)), 3600)
    mins = rem // 60
    if hours > 0 and mins > 0:
        return f"{hours} ساعت و {mins} دقیقه"
    if hours > 0:
        return f"{hours} ساعت"
    return f"{mins} دقیقه"


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
            "قابلیت لینک مستقیم روی این سرور تنظیم نشده است.",
            show_alert=True,
        )
        return

    user_id = callback.from_user.id if callback.from_user else None
    label_fa, pipeline_desc = ACTION_LABELS_FA.get(
        action_name, ACTION_LABELS_FA["link_orig"]
    )

    await callback.answer(f"⏳ در حال ساخت {label_fa}...")
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
        f"⏳ <b>در حال آماده‌سازی {label_fa}...</b>"
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
                "جمنای در دسترس نبود، با مدل پشتیبان (OpenRouter) ادامه می‌دم...",
            )

    async def _on_queue(pos: int) -> None:
        await animator.update(5, f"تو صف هستی، نفر {pos} ام")

    async def _on_shutdown(msg_fa: str) -> None:
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"⚠️ <b>{html.escape(msg_fa)}</b>")

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
                ttl_fa = _format_ttl_persian(result.ttl_sec)

                lines: list[str] = [
                    f"🔗 <b>{label_fa} آماده شد!</b>\n",
                    f"🎬 <b>عنوان:</b> {html.escape(result.title)}",
                    f"👤 <b>کانال:</b> <code>{html.escape(result.channel)}</code>",
                    f"📦 <b>حجم فایل:</b> <code>{size_mb} MB</code>",
                    f"⏳ <b>اعتبار لینک:</b> تا <code>{ttl_fa}</code> آینده\n",
                    "📋 <b>لینک مستقیم (کپی کن):</b>",
                    f"<code>{html.escape(result.url)}</code>\n",
                    "📱 <b>نحوه تماشا / دانلود:</b>",
                    "می‌تونی لینک بالا رو توی مرورگر بزنی تا دانلود بشه، یا توی برنامه <b>VLC</b> / <b>MX Player</b> قسمت <i>Open Network Stream</i> وارد کنی تا مستقیم و بدون دانلود کامل پخش بشه (پشتیبانی از جلو/عقب بردن و انتخاب زیرنویس فارسی/انگلیسی).\n",
                    "⚠️ <i>توجه: هر کسی که این لینک رو داشته باشه تا قبل از منقضی شدن می‌تونه فایل رو دانلود کنه.</i>",
                ]

                if result.approximate_timing:
                    lines.append(f"\n{APPROXIMATE_TIMING_LINE_FA}")
                if scope.openrouter_used and scope.openrouter_model:
                    lines.append(
                        OPENROUTER_FOOTER_TEMPLATE.format(
                            model=html.escape(scope.openrouter_model)
                        )
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
        await status_msg.edit_text(
            "⏱ <b>زمان آماده‌سازی لینک مستقیم بیش از حد مجاز طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>"
        )
    except Exception as exc:
        await animator.stop_heartbeat()
        logger.exception("Error in direct link action '%s': %s", action_name, exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action=action_name,
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(
            "❌ <b>ساخت لینک مستقیم با خطا مواجه شد، چند دقیقه دیگه دوباره امتحان کن.</b>"
        )
