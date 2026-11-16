import asyncio
import contextlib
import html
import logging
import time
from urllib.parse import quote

from aiogram import Bot, Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    Message,
    CallbackQuery,
    LinkPreviewOptions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    FSInputFile,
)

from config import settings
from services.analytics import Analytics
from services.audio_verifier import AudioVerifier, AudioTranscriptionResult
from services.gemini_extractor import GeminiExtractor, CaptionAnalysisResult
from services.job_manager import JobManager, JobRejectedError
from services.llm_router import LLMCallMeta, LLMRouter
from services.subtitle_service import SubtitleService, VideoSummarySchema
from services.transcript_provider import TranscriptFetchError
from services.youtube_searcher import YouTubeSearcher, YouTubeCandidate

logger = logging.getLogger(__name__)
router = Router(name="video_source_router")

OPENROUTER_FOOTER_TEMPLATE = (
    "\n<i>🤖 این نتیجه با مدل پشتیبان ساخته شد (جمنای در دسترس نبود)</i> "
    "<code>{model}</code>"
)
APPROXIMATE_TIMING_LINE_FA = (
    "⏱ <i>زمان‌بندی این زیرنویس تقریبیه (متن از روی صدا ساخته شده)</i>"
)


class VideoActionCallback(CallbackData, prefix="vact"):
    """CallbackData for inline subtitle, softsub, summary, and web player actions."""

    action: str  # "srt_fa" | "srt_bi" | "mux_cc" | "summary" | "web_play"
    video_id: str
    start_sec: int = 0


# ─────────────────────────────────────────────────────────────
# Progress UI & 3-Second Heartbeat Controllers
# ─────────────────────────────────────────────────────────────

class LiveProgressTracker:
    """
    Renders ONLY completed/skipped/warned steps and the single CURRENT step
    (never shows future steps and never uses the white square emoji),
    with a background 3-second heartbeat task showing elapsed seconds.
    """

    STEP_TITLES: dict[int, tuple[int, str]] = {
        1: (10, "تحلیل کپشن و استخراج سرنخ‌ها"),
        2: (35, "جستجو در یوتیوب و پیدا کردن کاندیداها"),
        3: (60, "استخراج صدای ویدیو"),
        4: (75, "تبدیل صدا به متن (Whisper)"),
        5: (85, "تطبیق متن صدا با زیرنویس یوتیوب"),
    }

    def __init__(self, status_message: Message) -> None:
        self.status_message = status_message
        self.percent = 0
        self.history: list[tuple[str, int, str]] = []  # (icon, pct, title)
        self.current_step: tuple[int, str] | None = None
        self.sub_activity: str = ""
        self.step_started_at: float = time.monotonic()
        self.advance_pct_ceiling: int | None = None
        self.last_rendered_text: str = ""
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._edit_lock = asyncio.Lock()

    @staticmethod
    def build_bar(percent: int, length: int = 10) -> str:
        filled = min(length, max(0, int(round((percent / 100) * length))))
        return "▓" * filled + "░" * (length - filled)

    def _compose_text(self) -> str:
        bar = self.build_bar(self.percent)
        lines: list[str] = [
            "🔎 <b>در حال پیدا کردن منبع اصلی ویدیو...</b>",
            f"<code>[{bar}] {self.percent}%</code>\n",
        ]

        for icon, step_pct, title in self.history:
            lines.append(f"{icon} [{step_pct}%] {html.escape(title)}")

        if self.current_step is not None:
            curr_pct, curr_title = self.current_step
            lines.append(f"⏳ <b>[{curr_pct}%] {html.escape(curr_title)}</b>")

        elapsed = int(time.monotonic() - self.step_started_at)
        if self.sub_activity:
            if elapsed >= 2:
                lines.append(
                    f"\n💡 {self.sub_activity} <code>({elapsed}s)</code>"
                )
            else:
                lines.append(f"\n💡 {self.sub_activity}")

        return "\n".join(lines)

    async def _safe_edit(self) -> None:
        async with self._edit_lock:
            new_text = self._compose_text()
            if new_text == self.last_rendered_text:
                return
            try:
                await self.status_message.edit_text(new_text)
                self.last_rendered_text = new_text
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after))
            except TelegramBadRequest:
                pass
            except Exception as exc:
                logger.debug("Progress edit ignored: %s", exc)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            if (
                self.advance_pct_ceiling is not None
                and self.percent < self.advance_pct_ceiling
            ):
                self.percent = min(self.advance_pct_ceiling, self.percent + 2)
            await self._safe_edit()

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        self._heartbeat_task = None

    async def begin_step(
        self,
        step_num: int,
        sub_activity: str,
        advance_ceiling: int | None = None,
    ) -> None:
        step_pct, title = self.STEP_TITLES[step_num]
        self.percent = max(self.percent, step_pct)
        self.current_step = (step_pct, title)
        self.sub_activity = sub_activity
        self.step_started_at = time.monotonic()
        self.advance_pct_ceiling = advance_ceiling
        await self._safe_edit()

    async def set_sub_activity(self, sub_activity: str) -> None:
        self.sub_activity = sub_activity
        await self._safe_edit()

    def finish_step(self, step_num: int, status: str = "done") -> None:
        step_pct, title = self.STEP_TITLES[step_num]
        icon_map = {
            "done": "✅",
            "skipped": "⏭",
            "warn": "⚠️",
        }
        icon = icon_map.get(status, "✅")
        self.history.append((icon, step_pct, title))
        self.current_step = None
        self.advance_pct_ceiling = None

    async def mark_complete(self, final_info: str) -> None:
        self.percent = 100
        self.current_step = None
        self.advance_pct_ceiling = None
        self.sub_activity = final_info
        self.step_started_at = time.monotonic()
        await self._safe_edit()


class ActionStatusAnimator:
    """Animated percent bar with a 3-second elapsed-time heartbeat for button actions."""

    def __init__(
        self, status_msg: Message, title: str, pipeline_header: str
    ) -> None:
        self.status_msg = status_msg
        self.title = title
        self.pipeline_header = pipeline_header
        self.percent = 5
        self.stage_text = "در حال آماده‌سازی..."
        self.started_at = time.monotonic()
        self.last_rendered = ""
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._edit_lock = asyncio.Lock()

    def _compose(self) -> str:
        bar = LiveProgressTracker.build_bar(self.percent)
        elapsed = int(time.monotonic() - self.started_at)
        return (
            f"🎬 <b>{html.escape(self.title)}</b>\n\n"
            f"🔄 {html.escape(self.pipeline_header)}\n"
            f"<code>[{bar}] {self.percent}%</code>\n\n"
            f"📡 <b>وضعیت:</b> {html.escape(self.stage_text)} <code>({elapsed}s)</code>"
        )

    async def _safe_edit(self) -> None:
        async with self._edit_lock:
            text = self._compose()
            if text == self.last_rendered:
                return
            try:
                await self.status_msg.edit_text(text)
                self.last_rendered = text
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after))
            except TelegramBadRequest:
                pass
            except Exception as exc:
                logger.debug("Animator edit ignored: %s", exc)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(3.0)
            await self._safe_edit()

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        self._heartbeat_task = None

    async def update(self, percent: int, stage_text: str) -> None:
        self.percent = max(self.percent, min(100, percent))
        self.stage_text = stage_text
        await self._safe_edit()


# ─────────────────────────────────────────────────────────────
# Basic Commands (/start & /help)
# ─────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    welcome_text = (
        "👋 <b>به ربات هوشمند پیدا کردن منبع و زیرنویس ویدیو خوش اومدی!</b>\n\n"
        "✨ <b>چطوری کار می‌کنه؟</b>\n"
        "کافیه هر پست ویدیویی از کانال‌های تلگرام رو برام <b>Forward</b> کنی:\n"
        "1️⃣ لینک اصلی ویدیو رو تو یوتیوب پیدا می‌کنم.\n"
        "2️⃣ صدای ویدیو رو بررسی می‌کنم تا <b>ثانیه‌ی دقیق شروع کلیپ</b> مشخص بشه.\n"
        "3️⃣ می‌تونی با یه کلیک <b>زیرنویس فارسی (.SRT)</b>، <b>ویدیوی سافت‌ساب</b> یا <b>خلاصه هوشمند</b> بگیری."
    )
    await message.answer(welcome_text)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    help_text = (
        "📖 <b>راهنمای استفاده از ربات:</b>\n\n"
        "• پست کانال تلگرام (ویدیو به همراه کپشن) رو برای ربات فوروارد کن.\n"
        "• بعد از پیدا شدن منبع، از دکمه‌های شیشه‌ای زیر پیام برای دانلود زیرنویس فارسی، "
        "ویدیوی سافت‌ساب، خلاصه نکات کلیدی یا پلیر تحت وب استفاده کن."
    )
    await message.answer(help_text)


# ─────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────

def _build_control_panel_keyboard(
    candidates: list[YouTubeCandidate],
) -> InlineKeyboardMarkup:
    best = candidates[0]
    vid_id = best.video_id
    start_sec = best.matched_timestamp_seconds or 0

    if best.matched_timestamp_seconds is not None:
        watch_label = f"▶️ تماشا از دقیقه {best.formatted_timestamp} در یوتیوب"
    else:
        watch_label = "▶️ تماشای ویدیوی اصلی در یوتیوب"

    share_text = quote(
        f"🎬 منبع اصلی ویدیو در یوتیوب:\n{best.title}\n🔗 {best.deep_link_url}"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=watch_label, url=best.deep_link_url)],
        [
            InlineKeyboardButton(
                text="📥 دانلود زیرنویس فارسی (.SRT)",
                callback_data=VideoActionCallback(
                    action="srt_fa", video_id=vid_id, start_sec=start_sec
                ).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎬 دانلود ویدیو با سافت‌ساب (CC فارسی)",
                callback_data=VideoActionCallback(
                    action="mux_cc", video_id=vid_id, start_sec=start_sec
                ).pack(),
            ),
        ],
    ]

    if settings.LINK_PUBLIC_BASE_URL:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔗 لینک سافت‌ساب",
                    callback_data=VideoActionCallback(
                        action="link_sub", video_id=vid_id, start_sec=start_sec
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="🔗 لینک اصلی",
                    callback_data=VideoActionCallback(
                        action="link_orig", video_id=vid_id, start_sec=start_sec
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="🎧 لینک صدا",
                    callback_data=VideoActionCallback(
                        action="link_audio", video_id=vid_id, start_sec=start_sec
                    ).pack(),
                ),
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="📝 نکات کلیدی و خلاصه هوشمند",
                    callback_data=VideoActionCallback(
                        action="summary", video_id=vid_id, start_sec=start_sec
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="🔄 زیرنویس دوزبانه (FA+EN)",
                    callback_data=VideoActionCallback(
                        action="srt_bi", video_id=vid_id, start_sec=start_sec
                    ).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 تماشای آنلاین با زیرنویس در وب",
                    callback_data=VideoActionCallback(
                        action="web_play", video_id=vid_id, start_sec=start_sec
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="📤 اشتراک‌گذاری",
                    url=f"https://t.me/share/url?url={quote(best.deep_link_url)}&text={share_text}",
                ),
            ],
        ]
    )

    thumb_row: list[InlineKeyboardButton] = []
    for idx, cand in enumerate(candidates[:3], start=1):
        thumb_row.append(
            InlineKeyboardButton(text=f"🖼 کاور گزینه {idx}", url=cand.thumbnail_url)
        )
    if thumb_row:
        rows.append(thumb_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _extract_context_from_message(message: Message) -> str:
    parts: list[str] = []
    if message.forward_origin and hasattr(message.forward_origin, "chat"):
        chat = getattr(message.forward_origin, "chat", None)
        if chat and getattr(chat, "title", None):
            parts.append(f"Forwarded from Telegram Channel: {chat.title}")

    if message.video and message.video.file_name:
        parts.append(f"Video File Name: {message.video.file_name}")
    elif message.document and message.document.file_name:
        parts.append(f"Document File Name: {message.document.file_name}")

    main_text = message.caption or message.text or ""
    if main_text.strip():
        parts.append(f"Post Caption/Text:\n{main_text.strip()}")

    return "\n\n".join(parts)


def _extract_media_file_info(message: Message) -> tuple[str | None, int | None]:
    if message.video:
        return message.video.file_id, message.video.file_size
    if message.document and message.document.mime_type:
        if message.document.mime_type.startswith(("video/", "audio/")):
            return message.document.file_id, message.document.file_size
    if message.audio:
        return message.audio.file_id, message.audio.file_size
    if message.voice:
        return message.voice.file_id, message.voice.file_size
    return None, None


def _format_final_response(
    candidates: list[YouTubeCandidate],
    analysis: CaptionAnalysisResult | None,
    transcription: AudioTranscriptionResult | None,
    warnings_fa: list[str],
    openrouter_model: str | None = None,
) -> str:
    """Format final result message, keeping total length safely under Telegram's 4096-character limit."""
    best = candidates[0]
    medals = ["🥇", "🥈", "🥉"]

    header_status = (
        "✅ <b>منبع اصلی ویدیو با تایید صوتی پیدا شد!</b>"
        if best.audio_match_score and best.audio_match_score >= 0.55
        else "✅ <b>بهترین منبع منطبق در یوتیوب پیدا شد!</b>"
    )

    lines: list[str] = [
        f"{header_status}\n",
        f"🏆 <b>انتخاب اول: {html.escape(best.title[:160])}</b>",
        f"👤 <b>کانال یوتیوب:</b> <code>{html.escape(best.channel[:80])}</code>",
        f"⏱ <b>مدت زمان کل:</b> <code>{best.formatted_duration}</code>",
        f"🎯 <b>اطمینان نهایی:</b> <b>{best.confidence_percent}%</b>",
    ]

    if best.formatted_timestamp:
        lines.append(
            f"📍 <b>نقطه شروع کلیپ شما در یوتیوب:</b> از دقیقه <code>{best.formatted_timestamp}</code> (ثانیه {best.matched_timestamp_seconds})"
        )

    if best.match_reasons:
        reasons_str = " | ".join(html.escape(r) for r in best.match_reasons[:3])
        lines.append(f"💡 <b>دلایل تطابق:</b> <i>{reasons_str}</i>")

    if best.matched_transcript_snippet:
        lines.append(
            f"🗣 <b>بخشی از دیالوگ تطبیق‌یافته:</b>\n"
            f"<blockquote>«{html.escape(best.matched_transcript_snippet[:180])}»</blockquote>"
        )

    lines.append(
        f"\n🔗 <b>لینک مستقیم تماشا:</b>\n{html.escape(best.deep_link_url)}\n"
        f"🖼 <a href='{html.escape(best.thumbnail_url)}'>مشاهده تصویر کاور (Thumbnail)</a>"
    )

    if len(candidates) > 1:
        lines.append("\n━━━━━━━━━━━━━━━━━━")
        lines.append("🗂 <b>کارت‌های مقایسه ۳ کاندیدای برتر:</b>\n")

        for idx, cand in enumerate(candidates[:3]):
            medal = medals[idx] if idx < len(medals) else f"{idx + 1}."
            ts_badge = (
                f" | 📍 دقیقه <code>{cand.formatted_timestamp}</code>"
                if cand.formatted_timestamp
                else ""
            )
            audio_badge = (
                f" | 🎙 صدا: <code>{cand.audio_match_percent}%</code>"
                if cand.audio_match_percent is not None
                else ""
            )

            lines.append(
                f"{medal} <b>{html.escape(cand.title[:110])}</b>\n"
                f"   📺 کانال: <code>{html.escape(cand.channel[:60])}</code>\n"
                f"   ⏱ زمان: <code>{cand.formatted_duration}</code> | 📊 اطمینان: <b>{cand.confidence_percent}%</b>{audio_badge}{ts_badge}\n"
                f"   🔗 <a href='{html.escape(cand.deep_link_url)}'>لینک ویدیو</a>  •  "
                f"🖼 <a href='{html.escape(cand.thumbnail_url)}'>پیش‌نمایش تامبنیل</a>\n"
            )

    if analysis and analysis.exact_entities:
        entities_str = ", ".join(html.escape(e) for e in analysis.exact_entities[:6])
        lines.append("───────────────")
        lines.append(f"🧩 <b>موجودیت‌ها:</b> <code>{entities_str}</code>")

    if transcription:
        lang_tag = (
            f"{transcription.detected_language} (ترجمه‌شده از دوبله)"
            if transcription.is_dubbed_translated
            else transcription.detected_language
        )
        lines.append(f"🎙 <b>زبان صدای تشخیص‌داده‌شده:</b> <code>{html.escape(lang_tag)}</code>")

    for w_line in warnings_fa:
        lines.append(f"⚠️ <i>{html.escape(w_line)}</i>")

    if openrouter_model:
        lines.append(
            OPENROUTER_FOOTER_TEMPLATE.format(
                model=html.escape(openrouter_model)
            )
        )

    lines.append(
        "\n🎛 <i>از دکمه‌های زیر برای دانلود زیرنویس، سافت‌ساب یا خلاصه استفاده کن:</i>"
    )
    out = "\n".join(lines)
    if len(out) > 4000:
        out = out[:3990] + "\n..."
    return out


# ─────────────────────────────────────────────────────────────
# Main Search Pipeline Handler
# ─────────────────────────────────────────────────────────────

@router.message(F.video | F.document | F.audio | F.voice | F.animation | F.text)
async def handle_incoming_post(
    message: Message,
    bot: Bot,
    gemini_extractor: GeminiExtractor,
    youtube_searcher: YouTubeSearcher,
    audio_verifier: AudioVerifier,
    subtitle_service: SubtitleService,
    analytics: Analytics,
    llm_router: LLMRouter,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    raw_caption = (message.caption or message.text or "").strip()
    file_id, file_size = _extract_media_file_info(message)

    if len(raw_caption) < 10 and not file_id:
        await message.reply(
            "⚠️ <b>ورودی کافی نیست!</b>\n"
            "لطفاً یک پست ویدیویی از کانال‌های تلگرام رو به همراه کپشن برام فوروارد کن."
        )
        return

    search_t0 = time.monotonic()
    status_msg = await message.reply("⏳ <i>در حال شروع جستجوی منبع ویدیو...</i>")
    progress = LiveProgressTracker(status_msg)
    progress.start_heartbeat()

    warnings_fa: list[str] = []

    async def _on_llm_switch(meta: LLMCallMeta) -> None:
        if meta.provider == "openrouter":
            await progress.set_sub_activity(
                "جمنای در دسترس نبود، با مدل پشتیبان (OpenRouter) ادامه می‌دم..."
            )

    try:
        async with llm_router.request_context(
            user_id=user_id, on_switch=_on_llm_switch
        ) as routing_scope:
            analysis: CaptionAnalysisResult | None = None
            transcription: AudioTranscriptionResult | None = None
            audio_bytes: bytes | None = None
            audio_extracted_early = False

            # ─────────────────────────────────────────────────────────
            # Step 1 [10%]: Caption Analysis (timeout: 50s)
            # ─────────────────────────────────────────────────────────
            if len(raw_caption) >= 10:
                await progress.begin_step(
                    1, "جمنای داره کپشن رو تحلیل می‌کنه..."
                )
                step_t0 = time.monotonic()
                logger.info("Step 'caption' started")
                try:
                    full_context = _extract_context_from_message(message)
                    analysis = await asyncio.wait_for(
                        gemini_extractor.extract_from_caption(full_context),
                        timeout=50.0,
                    )
                    dur = round(time.monotonic() - step_t0, 3)
                    st = "ok" if (analysis and analysis.search_queries) else "failed"
                    logger.info("Step 'caption' finished in %.2fs (status=%s)", dur, st)
                    await analytics.log_event(
                        user_id, "step_done", step="caption", seconds=dur, status=st
                    )
                    progress.finish_step(1, "done" if st == "ok" else "warn")
                except asyncio.TimeoutError:
                    dur = round(time.monotonic() - step_t0, 3)
                    logger.info("Step 'caption' finished in %.2fs (status=timeout)", dur)
                    await analytics.log_event(
                        user_id, "step_done", step="caption", seconds=dur, status="timeout"
                    )
                    progress.finish_step(1, "warn")
                    warnings_fa.append("تحلیل کپشن بیش از حد طول کشید (تایم‌اوت شد).")
            else:
                progress.finish_step(1, "skipped")

            # Fallback to audio transcript if caption analysis produced no queries and media exists
            if (not analysis or not analysis.search_queries) and file_id:
                audio_extracted_early = True
                await progress.begin_step(
                    3, "کپشن کافی نبود؛ در حال استخراج صدای ویدیو..."
                )
                step_t0 = time.monotonic()
                logger.info("Step 'audio' (early fallback) started")
                try:
                    audio_bytes = await asyncio.wait_for(
                        audio_verifier.extract_audio_bytes(bot, file_id, file_size),
                        timeout=40.0,
                    )
                    dur = round(time.monotonic() - step_t0, 3)
                    st = "ok" if audio_bytes else "failed"
                    logger.info("Step 'audio' finished in %.2fs (status=%s)", dur, st)
                    await analytics.log_event(
                        user_id, "step_done", step="audio", seconds=dur, status=st
                    )
                    progress.finish_step(3, "done" if audio_bytes else "warn")
                except asyncio.TimeoutError:
                    dur = round(time.monotonic() - step_t0, 3)
                    logger.info("Step 'audio' finished in %.2fs (status=timeout)", dur)
                    await analytics.log_event(
                        user_id, "step_done", step="audio", seconds=dur, status="timeout"
                    )
                    progress.finish_step(3, "warn")
                    warnings_fa.append("استخراج صدای ویدیو تایم‌اوت شد.")

                if audio_bytes:
                    await progress.begin_step(
                        4, "ویسپر داره صدا رو به متن تبدیل می‌کنه..."
                    )
                    step_t0 = time.monotonic()
                    logger.info("Step 'whisper' (early fallback) started")
                    try:
                        transcription = await asyncio.wait_for(
                            audio_verifier.transcribe_audio(audio_bytes),
                            timeout=45.0,
                        )
                        dur = round(time.monotonic() - step_t0, 3)
                        st = "ok" if transcription else "failed"
                        logger.info("Step 'whisper' finished in %.2fs (status=%s)", dur, st)
                        await analytics.log_event(
                            user_id, "step_done", step="whisper", seconds=dur, status=st
                        )
                        progress.finish_step(4, "done" if transcription else "warn")
                    except asyncio.TimeoutError:
                        dur = round(time.monotonic() - step_t0, 3)
                        logger.info("Step 'whisper' finished in %.2fs (status=timeout)", dur)
                        await analytics.log_event(
                            user_id, "step_done", step="whisper", seconds=dur, status="timeout"
                        )
                        progress.finish_step(4, "warn")
                        warnings_fa.append("تبدیل صدا به متن (Whisper) تایم‌اوت شد.")

                    if transcription and transcription.english_search_text:
                        await progress.set_sub_activity(
                            "ساخت کوئری جستجو از روی متن صدای ویدیو..."
                        )
                        with contextlib.suppress(asyncio.TimeoutError):
                            analysis = await asyncio.wait_for(
                                gemini_extractor.extract_from_caption(
                                    f"Video spoken transcript:\n{transcription.english_search_text}"
                                ),
                                timeout=50.0,
                            )

            if not analysis or not analysis.search_queries:
                await progress.stop_heartbeat()
                await analytics.log_event(
                    user_id, "search_failed", reason="no_clues"
                )
                if routing_scope.all_failed:
                    err_msg = (
                        "❌ <b>هیچ‌کدوم از مدل‌ها جواب ندادن، چند دقیقه دیگه دوباره امتحان کن.</b>"
                    )
                elif not file_id:
                    err_msg = (
                        "❌ <b>تحلیل متن به نتیجه نرسید؛ اگه فایل ویدیو رو داری، خود ویدیو رو با صداش بفرست.</b>"
                    )
                else:
                    err_msg = (
                        "❌ <b>سرنخ کافی توی کپشن یا صدای ویدیو پیدا نشد.</b>"
                    )
                await status_msg.edit_text(err_msg)
                return

            # ─────────────────────────────────────────────────────────
            # Step 2 [35%]: YouTube Search (timeout: 40s)
            # ─────────────────────────────────────────────────────────
            await progress.begin_step(
                2, "در حال جستجوی کاندیداها در یوتیوب..."
            )
            step_t0 = time.monotonic()
            logger.info("Step 'search' started")
            candidates: list[YouTubeCandidate] = []
            try:
                candidates = await asyncio.wait_for(
                    youtube_searcher.find_best_matches(analysis, top_k=4),
                    timeout=40.0,
                )
                dur = round(time.monotonic() - step_t0, 3)
                st = "ok" if candidates else "failed"
                logger.info("Step 'search' finished in %.2fs (status=%s, count=%d)", dur, st, len(candidates))
                await analytics.log_event(
                    user_id, "step_done", step="search", seconds=dur, status=st
                )
                progress.finish_step(2, "done" if candidates else "warn")
            except asyncio.TimeoutError:
                dur = round(time.monotonic() - step_t0, 3)
                logger.info("Step 'search' finished in %.2fs (status=timeout)", dur)
                await analytics.log_event(
                    user_id, "step_done", step="search", seconds=dur, status="timeout"
                )
                progress.finish_step(2, "warn")
                warnings_fa.append("جستجوی یوتیوب تایم‌اوت شد.")

            if not candidates:
                await progress.stop_heartbeat()
                await analytics.log_event(
                    user_id, "search_failed", reason="no_results"
                )
                await status_msg.edit_text(
                    "😕 <b>هیچ ویدیوی مشابهی در یوتیوب پیدا نشد.</b>\n"
                    "ممکنه منبع اصلی این کلیپ توی شبکه X (توییتر) یا TikTok باشه."
                )
                return

            # ─────────────────────────────────────────────────────────
            # Steps 3, 4, 5: Audio Extraction (40s), Whisper (45s), Transcript Matching (60s)
            # ─────────────────────────────────────────────────────────
            if file_id:
                if not audio_extracted_early:
                    await progress.begin_step(
                        3, "در حال استخراج ۴۰ ثانیه اول صدای ویدیو..."
                    )
                    step_t0 = time.monotonic()
                    logger.info("Step 'audio' started")
                    try:
                        audio_bytes = await asyncio.wait_for(
                            audio_verifier.extract_audio_bytes(bot, file_id, file_size),
                            timeout=40.0,
                        )
                        dur = round(time.monotonic() - step_t0, 3)
                        st = "ok" if audio_bytes else "failed"
                        logger.info("Step 'audio' finished in %.2fs (status=%s)", dur, st)
                        await analytics.log_event(
                            user_id, "step_done", step="audio", seconds=dur, status=st
                        )
                        progress.finish_step(3, "done" if audio_bytes else "warn")
                    except asyncio.TimeoutError:
                        dur = round(time.monotonic() - step_t0, 3)
                        logger.info("Step 'audio' finished in %.2fs (status=timeout)", dur)
                        await analytics.log_event(
                            user_id, "step_done", step="audio", seconds=dur, status="timeout"
                        )
                        progress.finish_step(3, "warn")
                        warnings_fa.append("مرحله‌ی استخراج صدای ویدیو تایم‌اوت شد.")

                if audio_bytes and not transcription:
                    await progress.begin_step(
                        4, "ویسپر داره صدا رو به متن تبدیل می‌کنه..."
                    )
                    step_t0 = time.monotonic()
                    logger.info("Step 'whisper' started")
                    try:
                        transcription = await asyncio.wait_for(
                            audio_verifier.transcribe_audio(audio_bytes),
                            timeout=45.0,
                        )
                        dur = round(time.monotonic() - step_t0, 3)
                        st = "ok" if transcription else "failed"
                        logger.info("Step 'whisper' finished in %.2fs (status=%s)", dur, st)
                        await analytics.log_event(
                            user_id, "step_done", step="whisper", seconds=dur, status=st
                        )
                        progress.finish_step(4, "done" if transcription else "warn")
                    except asyncio.TimeoutError:
                        dur = round(time.monotonic() - step_t0, 3)
                        logger.info("Step 'whisper' finished in %.2fs (status=timeout)", dur)
                        await analytics.log_event(
                            user_id, "step_done", step="whisper", seconds=dur, status="timeout"
                        )
                        progress.finish_step(4, "warn")
                        warnings_fa.append("مرحله‌ی تبدیل صدا به متن (Whisper) تایم‌اوت شد.")

                if transcription and transcription.english_search_text:
                    # Percent inside transcript matching advances by time estimation capped at 96%
                    # because AudioVerifier.verify_and_rerank_candidates fetches all candidates in parallel.
                    await progress.begin_step(
                        5,
                        f"در حال تطبیق زیرنویس با {len(candidates)} کاندیدای برتر...",
                        advance_ceiling=96,
                    )
                    step_t0 = time.monotonic()
                    logger.info("Step 'match' started")
                    try:
                        candidates = await asyncio.wait_for(
                            audio_verifier.verify_and_rerank_candidates(
                                candidates, transcription
                            ),
                            timeout=60.0,
                        )
                        dur = round(time.monotonic() - step_t0, 3)
                        logger.info("Step 'match' finished in %.2fs (status=ok)", dur)
                        await analytics.log_event(
                            user_id, "step_done", step="match", seconds=dur, status="ok"
                        )
                        progress.finish_step(5, "done")
                        if audio_verifier.last_fallback_notice_fa:
                            warnings_fa.append(audio_verifier.last_fallback_notice_fa)
                    except asyncio.TimeoutError:
                        dur = round(time.monotonic() - step_t0, 3)
                        logger.info("Step 'match' finished in %.2fs (status=timeout)", dur)
                        await analytics.log_event(
                            user_id, "step_done", step="match", seconds=dur, status="timeout"
                        )
                        progress.finish_step(5, "warn")
                        warnings_fa.append(
                            "تطبیق زیرنویس یوتیوب تایم‌اوت شد؛ رتبه‌بندی بر اساس اطلاعات کپشنه."
                        )
                else:
                    progress.finish_step(5, "skipped")
            else:
                progress.finish_step(3, "skipped")
                progress.finish_step(4, "skipped")
                progress.finish_step(5, "skipped")

            await progress.mark_complete("رتبه‌بندی نهایی آماده شد!")
            await progress.stop_heartbeat()

            top_candidates = candidates[:3]
            for cand in top_candidates:
                subtitle_service.cache_video_metadata(
                    video_id=cand.video_id,
                    title=cand.title,
                    channel=cand.channel,
                    duration_seconds=cand.duration_seconds,
                    thumbnail_url=cand.thumbnail_url,
                )

            best = top_candidates[0]
            total_search_sec = round(time.monotonic() - search_t0, 3)
            await analytics.log_event(
                user_id,
                "search_done",
                found=bool(best.confidence_score >= settings.CONFIDENCE_THRESHOLD),
                confidence=round(best.confidence_score, 2),
                has_audio=bool(file_id is not None),
                audio_match=(
                    round(best.audio_match_score, 2)
                    if best.audio_match_score is not None
                    else None
                ),
                dubbed=bool(
                    transcription.is_dubbed_translated if transcription else False
                ),
                candidates=len(top_candidates),
                seconds=total_search_sec,
            )

            final_html = _format_final_response(
                candidates=top_candidates,
                analysis=analysis,
                transcription=transcription,
                warnings_fa=warnings_fa,
                openrouter_model=(
                    routing_scope.openrouter_model
                    if routing_scope.openrouter_used
                    else None
                ),
            )
            reply_kb = _build_control_panel_keyboard(top_candidates)

            await status_msg.edit_text(
                final_html,
                reply_markup=reply_kb,
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    url=best.thumbnail_url,
                    prefer_large_media=True,
                    show_above_text=True,
                ),
            )

    except Exception as exc:
        await progress.stop_heartbeat()
        logger.exception("Unhandled error in video_handler: %s", exc)
        await analytics.log_event(
            user_id, "search_failed", reason="exception"
        )
        await status_msg.edit_text(
            "⚠️ <b>خطایی موقع پردازش پیش اومد، چند لحظه دیگه دوباره امتحان کن.</b>"
        )


# ─────────────────────────────────────────────────────────────
# CallbackQuery Handlers: SRT, Softsub, Summary, Web Player
# ─────────────────────────────────────────────────────────────

@router.callback_query(VideoActionCallback.filter(F.action.in_({"srt_fa", "srt_bi"})))
async def cb_download_srt(
    callback: CallbackQuery,
    callback_data: VideoActionCallback,
    subtitle_service: SubtitleService,
    analytics: Analytics,
    llm_router: LLMRouter,
    job_manager: JobManager,
) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    action_name = callback_data.action
    is_bilingual = action_name == "srt_bi"
    mode = "bilingual" if is_bilingual else "fa"
    suffix = "_FA_EN.srt" if is_bilingual else "_FA.srt"
    mode_title = (
        "زیرنویس دوزبانه (فارسی + انگلیسی)"
        if is_bilingual
        else "زیرنویس فارسی (.SRT)"
    )

    await callback.answer(f"⏳ در حال آماده‌سازی {mode_title}...")
    if not callback.message:
        return

    meta = await subtitle_service.get_video_metadata(callback_data.video_id)
    status_msg = await callback.message.reply(
        f"⏳ <b>در حال آماده‌سازی {mode_title}...</b>"
    )
    animator = ActionStatusAnimator(
        status_msg=status_msg,
        title=meta.title,
        pipeline_header="دریافت تایم‌کدها -> ترجمه هوشمند -> ساخت فایل SRT",
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
        await animator.update(10, f"تو صف هستی، نفر {pos} ام")

    async def _on_shutdown(msg_fa: str) -> None:
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"⚠️ <b>{html.escape(msg_fa)}</b>")

    try:
        async with job_manager.run_heavy_job(
            user_id=user_id,
            kind="translation",
            on_queue_position=_on_queue,
            on_shutdown_notify=_on_shutdown,
        ):
            async with llm_router.request_context(
                user_id=user_id, on_switch=_on_switch
            ) as scope:
                logger.info("Action '%s' started for video=%s", action_name, callback_data.video_id)
                segments = await asyncio.wait_for(
                    subtitle_service.get_or_create_translated_segments(
                        video_id=callback_data.video_id,
                        progress_cb=animator.update,
                    ),
                    timeout=90.0,
                )

                srt_content = subtitle_service.build_srt_content(segments, mode=mode)
                srt_bytes = srt_content.encode("utf-8")
                file_name = subtitle_service.sanitize_filename(meta.title, suffix=suffix)
                preview_block = subtitle_service.get_preview_lines(segments, count=3)

                caption_lines = [
                    f"✅ <b>فایل {mode_title} آماده شد!</b>\n",
                    f"🎬 <b>عنوان:</b> {html.escape(meta.title)}",
                    f"👤 <b>کانال:</b> <code>{html.escape(meta.channel)}</code>",
                    f"📊 <b>تعداد خطوط زیرنویس:</b> <code>{len(segments)} خط</code>\n",
                    f"👁‍🗨 <b>پیش‌نمایش ۳ دیالوگ اول:</b>",
                    f"<blockquote>{preview_block}</blockquote>",
                ]
                if subtitle_service.is_approximate(callback_data.video_id):
                    caption_lines.append(f"\n{APPROXIMATE_TIMING_LINE_FA}")
                if scope.openrouter_used and scope.openrouter_model:
                    caption_lines.append(
                        OPENROUTER_FOOTER_TEMPLATE.format(
                            model=html.escape(scope.openrouter_model)
                        )
                    )

                await animator.stop_heartbeat()
                input_doc = BufferedInputFile(file=srt_bytes, filename=file_name)
                await callback.message.reply_document(
                    document=input_doc,
                    caption="\n".join(caption_lines),
                )
                await status_msg.delete()

                dur = round(time.monotonic() - t0, 3)
                logger.info("Action '%s' finished in %.2fs (ok=True)", action_name, dur)
                await analytics.log_event(
                    user_id, "action_done", action=action_name, seconds=dur, ok=True
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
        logger.warning("Action '%s' timed out after %.2fs", action_name, dur)
        await analytics.log_event(
            user_id, "action_failed", action=action_name, error_type="TimeoutError"
        )
        await status_msg.edit_text(
            "⏱ <b>زمان ترجمه زیرنویس بیش از حد مجاز (۹۰ ثانیه) طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>"
        )
    except Exception as exc:
        await animator.stop_heartbeat()
        logger.exception("Error in action '%s': %s", action_name, exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action=action_name,
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(
            "❌ <b>هیچ‌کدوم از مدل‌ها جواب ندادن، چند دقیقه دیگه دوباره امتحان کن.</b>"
        )


@router.callback_query(VideoActionCallback.filter(F.action == "mux_cc"))
async def cb_download_softsub_video(
    callback: CallbackQuery,
    callback_data: VideoActionCallback,
    subtitle_service: SubtitleService,
    analytics: Analytics,
    llm_router: LLMRouter,
    job_manager: JobManager,
) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    await callback.answer("🎬 فرآیند ساخت ویدیوی سافت‌ساب شروع شد...")
    if not callback.message:
        return

    meta = await subtitle_service.get_video_metadata(callback_data.video_id)

    # Pre-work duration check before queuing
    try:
        job_manager.check_softsub_duration(meta.duration_seconds, user_id=user_id)
    except JobRejectedError as exc:
        await callback.message.reply(f"⚠️ <b>{html.escape(exc.user_message_fa)}</b>")
        return

    status_msg = await callback.message.reply(
        "⏳ <b>در حال آماده‌سازی ویدیوی سافت‌ساب...</b>"
    )
    animator = ActionStatusAnimator(
        status_msg=status_msg,
        title=meta.title,
        pipeline_header="ترجمه زیرنویس -> دریافت ویدیو -> الصاق سافت‌ساب",
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

    try:
        async with job_manager.run_heavy_job(
            user_id=user_id,
            kind="video",
            on_queue_position=_on_queue,
            on_shutdown_notify=_on_shutdown,
            require_disk_check=True,
        ):
            with job_manager.create_job_dir(f"mux_{callback_data.video_id}_") as work_dir:
                async with llm_router.request_context(
                    user_id=user_id, on_switch=_on_switch
                ) as scope:
                    logger.info("Action 'mux_cc' started for video=%s", callback_data.video_id)
                    mux_result = await asyncio.wait_for(
                        subtitle_service.create_softsub_video(
                            video_id=callback_data.video_id,
                            work_dir=work_dir,
                            progress_cb=animator.update,
                        ),
                        timeout=180.0,
                    )

                    size_mb = round(mux_result.file_size_bytes / (1024 * 1024), 2)
                    await animator.update(
                        99, f"در حال آپلود ویدیوی سافت‌ساب در تلگرام ({size_mb:.1f} MB)..."
                    )

                    video_file = FSInputFile(
                        path=mux_result.muxed_video_path,
                        filename=subtitle_service.sanitize_filename(
                            mux_result.title, suffix="_SoftSub_FA.mp4"
                        ),
                    )
                    thumb_file = (
                        FSInputFile(path=mux_result.thumbnail_path)
                        if mux_result.thumbnail_path
                        else None
                    )

                    caption_lines = [
                        f"🎬 <b>{html.escape(mux_result.title)}</b>",
                        f"👤 <b>کانال:</b> <code>{html.escape(mux_result.channel)}</code>",
                        f"📦 <b>حجم فایل:</b> <code>{size_mb:.1f} MB</code>\n",
                        "✨ <b>ویژگی سافت‌ساب (CC):</b>",
                        "زیرنویس فارسی و انگلیسی داخل ویدیو قرار گرفته؛ توی پلیر تلگرام روی دکمه <b>CC</b> بزن تا فعال بشه.",
                    ]
                    if mux_result.approximate_timing:
                        caption_lines.append(f"\n{APPROXIMATE_TIMING_LINE_FA}")
                    if scope.openrouter_used and scope.openrouter_model:
                        caption_lines.append(
                            OPENROUTER_FOOTER_TEMPLATE.format(
                                model=html.escape(scope.openrouter_model)
                            )
                        )

                    await animator.stop_heartbeat()
                    await callback.message.reply_video(
                        video=video_file,
                        caption="\n".join(caption_lines),
                        duration=mux_result.duration_seconds or None,
                        width=mux_result.width or None,
                        height=mux_result.height or None,
                        thumbnail=thumb_file,
                        supports_streaming=True,
                    )
                    await status_msg.delete()

                    dur = round(time.monotonic() - t0, 3)
                    logger.info(
                        "Action 'mux_cc' finished in %.2fs (ok=True, size_mb=%.2f)",
                        dur,
                        size_mb,
                    )
                    await analytics.log_event(
                        user_id,
                        "action_done",
                        action="mux_cc",
                        seconds=dur,
                        ok=True,
                        size_mb=size_mb,
                    )

    except JobRejectedError as exc:
        await animator.stop_heartbeat()
        await status_msg.edit_text(f"⚠️ <b>{html.escape(exc.user_message_fa)}</b>")
    except TranscriptFetchError as exc:
        await animator.stop_heartbeat()
        await analytics.log_event(
            user_id, "action_failed", action="mux_cc", error_type=exc.error_kind
        )
        await status_msg.edit_text(f"❌ <b>{html.escape(exc.user_message_fa)}</b>")
    except asyncio.TimeoutError:
        await animator.stop_heartbeat()
        dur = round(time.monotonic() - t0, 3)
        logger.warning("Action 'mux_cc' timed out after %.2fs", dur)
        await analytics.log_event(
            user_id, "action_failed", action="mux_cc", error_type="TimeoutError"
        )
        await status_msg.edit_text(
            "⏱ <b>ساخت ویدیوی سافت‌ساب بیش از حد مجاز (۱۸۰ ثانیه) طول کشید. می‌تونی از دکمه‌ی دانلود زیرنویس (.SRT) استفاده کنی.</b>"
        )
    except TelegramBadRequest as exc:
        await animator.stop_heartbeat()
        logger.error("Telegram upload error for softsub video: %s", exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action="mux_cc",
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(
            "⚠️ <b>حجم ویدیو بیشتر از سقف ۵۰ مگابایت تلگرامه. می‌تونی فایل زیرنویس فارسی (.SRT) رو از دکمه‌ی بالا دانلود کنی.</b>"
        )
    except Exception as exc:
        await animator.stop_heartbeat()
        logger.exception("Error in action 'mux_cc': %s", exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action="mux_cc",
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(
            "❌ <b>ساخت ویدیوی سافت‌ساب با خطا مواجه شد، چند دقیقه دیگه دوباره امتحان کن.</b>"
        )


@router.callback_query(VideoActionCallback.filter(F.action == "summary"))
async def cb_smart_summary(
    callback: CallbackQuery,
    callback_data: VideoActionCallback,
    subtitle_service: SubtitleService,
    analytics: Analytics,
    llm_router: LLMRouter,
    job_manager: JobManager,
) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    await callback.answer("📝 در حال تحلیل ویدیو و استخراج نکات کلیدی...")
    if not callback.message:
        return

    meta = await subtitle_service.get_video_metadata(callback_data.video_id)
    status_msg = await callback.message.reply(
        "⏳ <b>در حال تحلیل هوشمند محتوای ویدیو...</b>"
    )
    animator = ActionStatusAnimator(
        status_msg=status_msg,
        title=meta.title,
        pipeline_header="استخراج متن -> تحلیل محتوا -> تدوین فصل‌ها",
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
        await animator.update(10, f"تو صف هستی، نفر {pos} ام")

    async def _on_shutdown(msg_fa: str) -> None:
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"⚠️ <b>{html.escape(msg_fa)}</b>")

    try:
        async with job_manager.run_heavy_job(
            user_id=user_id,
            kind="translation",
            on_queue_position=_on_queue,
            on_shutdown_notify=_on_shutdown,
        ):
            async with llm_router.request_context(
                user_id=user_id, on_switch=_on_switch
            ) as scope:
                logger.info("Action 'summary' started for video=%s", callback_data.video_id)
                summary: VideoSummarySchema = await asyncio.wait_for(
                    subtitle_service.generate_smart_summary(
                        video_id=callback_data.video_id,
                        progress_cb=animator.update,
                    ),
                    timeout=90.0,
                )

                lines: list[str] = [
                    "📝 <b>خلاصه هوشمند و نکات کلیدی ویدیو</b>",
                    f"🎬 <b>{html.escape(meta.title)}</b>",
                    f"👤 کانال: <code>{html.escape(meta.channel)}</code>",
                    "━━━━━━━━━━━━━━━━━━\n",
                    f"🎯 <b>تیتر کلیدی:</b>\n<b>{html.escape(summary.headline_fa)}</b>\n",
                    f"💡 <b>خلاصه مدیریتی:</b>\n<blockquote>{html.escape(summary.tldr_fa)}</blockquote>\n",
                    "🔑 <b>مهم‌ترین نکات ویدیو:</b>",
                ]

                for idx, point in enumerate(summary.key_takeaways_fa, start=1):
                    lines.append(f"  {idx}. {html.escape(point)}")

                if summary.technical_terms:
                    lines.append("\n🧪 <b>اصطلاحات و مدل‌های تخصصی:</b>")
                    for term in summary.technical_terms[:6]:
                        lines.append(f"  • <code>{html.escape(term)}</code>")

                if summary.chapters:
                    lines.append("\n⏱ <b>فصل‌بندی زمانی ویدیو:</b>")
                    for ch in summary.chapters[:8]:
                        mins, secs = divmod(max(0, ch.timestamp_seconds), 60)
                        time_label = f"{mins:02d}:{secs:02d}"
                        deep_url = f"https://youtu.be/{callback_data.video_id}?t={max(0, ch.timestamp_seconds)}"
                        lines.append(
                            f"📍 <a href='{deep_url}'><b>[{time_label}]</b></a> <b>{html.escape(ch.title_fa)}</b>\n"
                            f"   └ <i>{html.escape(ch.summary_fa)}</i>"
                        )

                if scope.openrouter_used and scope.openrouter_model:
                    lines.append(
                        OPENROUTER_FOOTER_TEMPLATE.format(
                            model=html.escape(scope.openrouter_model)
                        )
                    )

                await animator.stop_heartbeat()
                out_text = "\n".join(lines)
                if len(out_text) > 4000:
                    out_text = out_text[:3990] + "\n..."
                await status_msg.edit_text(
                    out_text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )

                dur = round(time.monotonic() - t0, 3)
                logger.info("Action 'summary' finished in %.2fs (ok=True)", dur)
                await analytics.log_event(
                    user_id, "action_done", action="summary", seconds=dur, ok=True
                )

    except JobRejectedError as exc:
        await animator.stop_heartbeat()
        await status_msg.edit_text(f"⚠️ <b>{html.escape(exc.user_message_fa)}</b>")
    except TranscriptFetchError as exc:
        await animator.stop_heartbeat()
        await analytics.log_event(
            user_id, "action_failed", action="summary", error_type=exc.error_kind
        )
        await status_msg.edit_text(f"❌ <b>{html.escape(exc.user_message_fa)}</b>")
    except asyncio.TimeoutError:
        await animator.stop_heartbeat()
        dur = round(time.monotonic() - t0, 3)
        logger.warning("Action 'summary' timed out after %.2fs", dur)
        await analytics.log_event(
            user_id, "action_failed", action="summary", error_type="TimeoutError"
        )
        await status_msg.edit_text(
            "⏱ <b>تولید خلاصه بیش از حد مجاز (۹۰ ثانیه) طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>"
        )
    except Exception as exc:
        await animator.stop_heartbeat()
        logger.exception("Error in action 'summary': %s", exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action="summary",
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(
            "❌ <b>هیچ‌کدوم از مدل‌ها جواب ندادن، چند دقیقه دیگه دوباره امتحان کن.</b>"
        )


@router.callback_query(VideoActionCallback.filter(F.action == "web_play"))
async def cb_interactive_web_player(
    callback: CallbackQuery,
    callback_data: VideoActionCallback,
    subtitle_service: SubtitleService,
    analytics: Analytics,
    llm_router: LLMRouter,
    job_manager: JobManager,
) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    await callback.answer("🌐 در حال ساخت پلیر وب تعاملی با زیرنویس فارسی...")
    if not callback.message:
        return

    meta = await subtitle_service.get_video_metadata(callback_data.video_id)
    status_msg = await callback.message.reply(
        "⏳ <b>در حال ساخت پلیر وب تعاملی...</b>"
    )
    animator = ActionStatusAnimator(
        status_msg=status_msg,
        title=meta.title,
        pipeline_header="آماده‌سازی زیرنویس فارسی -> ساخت پلیر HTML5",
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
        await animator.update(10, f"تو صف هستی، نفر {pos} ام")

    async def _on_shutdown(msg_fa: str) -> None:
        with contextlib.suppress(Exception):
            await status_msg.edit_text(f"⚠️ <b>{html.escape(msg_fa)}</b>")

    try:
        async with job_manager.run_heavy_job(
            user_id=user_id,
            kind="translation",
            on_queue_position=_on_queue,
            on_shutdown_notify=_on_shutdown,
        ):
            async with llm_router.request_context(
                user_id=user_id, on_switch=_on_switch
            ) as scope:
                logger.info("Action 'web_play' started for video=%s", callback_data.video_id)
                segments = await asyncio.wait_for(
                    subtitle_service.get_or_create_translated_segments(
                        video_id=callback_data.video_id,
                        progress_cb=animator.update,
                    ),
                    timeout=90.0,
                )

                html_player_str = subtitle_service.build_interactive_web_player_html(
                    meta=meta,
                    segments=segments,
                    start_seconds=callback_data.start_sec,
                )
                html_bytes = html_player_str.encode("utf-8")
                player_filename = subtitle_service.sanitize_filename(
                    meta.title, suffix="_WebPlayer_FA.html"
                )

                yt_cc_url = (
                    f"https://www.youtube.com/watch?v={callback_data.video_id}"
                    f"&t={callback_data.start_sec}s&cc_load_policy=1&cc_lang_pref=fa"
                )

                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="▶️ باز کردن مستقیم در یوتیوب (CC فعال)",
                                url=yt_cc_url,
                            )
                        ]
                    ]
                )

                caption_lines = [
                    "🌐 <b>پلیر وب تعاملی دوزبانه آماده شد!</b>\n",
                    f"🎬 <b>{html.escape(meta.title)}</b>\n",
                    "✨ <b>نحوه استفاده:</b>",
                    "فایل <code>.html</code> بالا رو توی مرورگر گوشی یا کامپیوتر باز کن تا ویدیو با زیرنویس زنده فارسی و لیست دیالوگ‌های قابل کلیک پخش بشه.",
                ]
                if subtitle_service.is_approximate(callback_data.video_id):
                    caption_lines.append(f"\n{APPROXIMATE_TIMING_LINE_FA}")
                if scope.openrouter_used and scope.openrouter_model:
                    caption_lines.append(
                        OPENROUTER_FOOTER_TEMPLATE.format(
                            model=html.escape(scope.openrouter_model)
                        )
                    )

                await animator.stop_heartbeat()
                doc = BufferedInputFile(file=html_bytes, filename=player_filename)
                await callback.message.reply_document(
                    document=doc,
                    caption="\n".join(caption_lines),
                    reply_markup=kb,
                )
                await status_msg.delete()

                dur = round(time.monotonic() - t0, 3)
                logger.info("Action 'web_play' finished in %.2fs (ok=True)", dur)
                await analytics.log_event(
                    user_id, "action_done", action="web_play", seconds=dur, ok=True
                )

    except JobRejectedError as exc:
        await animator.stop_heartbeat()
        await status_msg.edit_text(f"⚠️ <b>{html.escape(exc.user_message_fa)}</b>")
    except TranscriptFetchError as exc:
        await animator.stop_heartbeat()
        await analytics.log_event(
            user_id, "action_failed", action="web_play", error_type=exc.error_kind
        )
        await status_msg.edit_text(f"❌ <b>{html.escape(exc.user_message_fa)}</b>")
    except asyncio.TimeoutError:
        await animator.stop_heartbeat()
        dur = round(time.monotonic() - t0, 3)
        logger.warning("Action 'web_play' timed out after %.2fs", dur)
        await analytics.log_event(
            user_id, "action_failed", action="web_play", error_type="TimeoutError"
        )
        await status_msg.edit_text(
            "⏱ <b>ساخت پلیر وب بیش از حد مجاز (۹۰ ثانیه) طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>"
        )
    except Exception as exc:
        await animator.stop_heartbeat()
        logger.exception("Error in action 'web_play': %s", exc)
        await analytics.log_event(
            user_id,
            "action_failed",
            action="web_play",
            error_type=exc.__class__.__name__,
        )
        await status_msg.edit_text(
            "❌ <b>هیچ‌کدوم از مدل‌ها جواب ندادن، چند دقیقه دیگه دوباره امتحان کن.</b>"
        )
