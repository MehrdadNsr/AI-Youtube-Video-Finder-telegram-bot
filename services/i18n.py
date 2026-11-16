import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

SupportedLang = str  # "fa" | "en"

# Context variable holding the resolved language for the current Telegram update
current_lang_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_lang", default="fa"
)


def get_current_lang() -> str:
    """Return the active language code ('fa' or 'en') for the current async context."""
    lang = current_lang_var.get()
    return lang if lang in ("fa", "en") else "fa"


def set_current_lang(lang: str) -> contextvars.Token[str]:
    """Set the active language code in the current async context."""
    normalized = lang if lang in ("fa", "en") else "fa"
    return current_lang_var.set(normalized)


def resolve_user_lang(
    stored_lang: str | None = None,
    default_lang: str = "en",
) -> str:
    """
    Resolve language for a user:
    1. Explicit choice stored in SQLite (`stored_lang` in {'fa', 'en'}), set via /start or /lang
    2. Fallback to `default_lang` (settings.DEFAULT_LANG, English by default)
    The Telegram client language is intentionally ignored: users pick their language on /start.
    """
    if stored_lang in ("fa", "en"):
        return stored_lang
    return default_lang if default_lang in ("fa", "en") else "en"


STRINGS: dict[str, dict[str, str]] = {
    # ─── Commands: /start, /help, /lang ───
    "welcome_message": {
        "fa": (
            "👋 <b>به ربات هوشمند پیدا کردن منبع و زیرنویس ویدیو خوش اومدی!</b>\n\n"
            "✨ <b>چطوری کار می‌کنه؟</b>\n"
            "کافیه هر پست ویدیویی از کانال‌های تلگرام رو برام <b>Forward</b> کنی:\n"
            "1️⃣ لینک اصلی ویدیو رو تو یوتیوب پیدا می‌کنم.\n"
            "2️⃣ صدای ویدیو رو بررسی می‌کنم تا <b>ثانیه‌ی دقیق شروع کلیپ</b> مشخص بشه.\n"
            "3️⃣ می‌تونی با یه کلیک <b>زیرنویس فارسی (.SRT)</b>، <b>ویدیوی سافت‌ساب</b> یا <b>خلاصه هوشمند</b> بگیری.\n\n"
            "🌐 برای تغییر زبان ربات از دستور /lang استفاده کن."
        ),
        "en": (
            "👋 <b>Welcome to Video Source Finder Bot!</b>\n\n"
            "✨ <b>How does it work?</b>\n"
            "Simply <b>Forward</b> any video post from Telegram channels to me:\n"
            "1️⃣ I find the original video source on YouTube.\n"
            "2️⃣ I analyze the audio track to find the <b>exact start timestamp</b> of the clip.\n"
            "3️⃣ With one tap, you can get <b>Persian subtitles (.SRT)</b>, a <b>softsubbed video</b>, or a <b>smart summary</b>.\n\n"
            "🌐 Use /lang at any time to switch the bot language."
        ),
    },
    "help_message": {
        "fa": (
            "📖 <b>راهنمای استفاده از ربات:</b>\n\n"
            "• پست کانال تلگرام (ویدیو به همراه کپشن) رو برای ربات فوروارد کن.\n"
            "• بعد از پیدا شدن منبع، از دکمه‌های شیشه‌ای زیر پیام برای دانلود زیرنویس فارسی، "
            "ویدیوی سافت‌ساب، خلاصه نکات کلیدی یا پلیر تحت وب استفاده کن.\n"
            "• با دستور /lang می‌تونی زبان منوها و پیام‌های ربات رو بین فارسی و انگلیسی تغییر بدی.\n"
            "• با دستور /version می‌تونی وضعیت سرور و نسخه‌ی ربات رو ببینی."
        ),
        "en": (
            "📖 <b>How to Use the Bot:</b>\n\n"
            "• Forward a Telegram channel post (video with caption) to the bot.\n"
            "• Once the original YouTube source is found, use the inline buttons below the message to download "
            "Persian subtitles (.SRT), a softsubbed video, key takeaways summary, or the interactive web player.\n"
            "• Use /lang to switch the bot's interface language between Persian and English.\n"
            "• Use /version to check the bot version and server status."
        ),
    },
    "lang_choose_prompt": {
        "fa": "🌐 <b>زبان رابط کاربری ربات را انتخاب کنید:</b>\n<i>Please select your preferred interface language:</i>",
        "en": "🌐 <b>Please select your preferred interface language:</b>\n<i>زبان رابط کاربری ربات را انتخاب کنید:</i>",
    },
    "lang_btn_fa": {
        "fa": "🇮🇷 فارسی",
        "en": "🇮🇷 فارسی",
    },
    "lang_btn_en": {
        "fa": "🇬🇧 English",
        "en": "🇬🇧 English",
    },
    "lang_switched_confirm": {
        "fa": "✅ زبان ربات به <b>فارسی</b> تغییر کرد.",
        "en": "✅ Bot language has been set to <b>English</b>.",
    },

    # ─── Telegram Menu Button command descriptions (max 256 chars, no HTML) ───
    "cmd_desc_start": {
        "fa": "شروع و معرفی ربات",
        "en": "Start and bot introduction",
    },
    "cmd_desc_help": {
        "fa": "راهنمای استفاده",
        "en": "How to use the bot",
    },
    "cmd_desc_lang": {
        "fa": "تغییر زبان / Change language",
        "en": "Change language / تغییر زبان",
    },
    "cmd_desc_version": {
        "fa": "نسخه و وضعیت سرور",
        "en": "Bot version and server status",
    },
    "cmd_desc_stats": {
        "fa": "آمار استفاده (فقط ادمین)",
        "en": "Usage analytics (admin only)",
    },
    "cmd_desc_stats_export": {
        "fa": "خروجی JSON آمار (فقط ادمین)",
        "en": "Analytics JSON export (admin only)",
    },

    # ─── Progress Tracker Steps & Sub-activities ───
    "progress_header": {
        "fa": "🔎 <b>در حال پیدا کردن منبع اصلی ویدیو...</b>",
        "en": "🔎 <b>Finding the original video source...</b>",
    },
    "step_1_title": {
        "fa": "تحلیل کپشن و استخراج سرنخ‌ها",
        "en": "Analyzing caption and extracting clues",
    },
    "step_2_title": {
        "fa": "جستجو در یوتیوب و پیدا کردن کاندیداها",
        "en": "Searching YouTube and scoring candidates",
    },
    "step_3_title": {
        "fa": "استخراج صدای ویدیو",
        "en": "Extracting video audio track",
    },
    "step_4_title": {
        "fa": "تبدیل صدا به متن (Whisper)",
        "en": "Transcribing audio with Whisper",
    },
    "step_5_title": {
        "fa": "تطبیق متن صدا با زیرنویس یوتیوب",
        "en": "Matching audio against YouTube transcripts",
    },
    "init_search_status": {
        "fa": "⏳ <i>در حال شروع جستجوی منبع ویدیو...</i>",
        "en": "⏳ <i>Starting video source search pipeline...</i>",
    },
    "sub_analyzing_caption": {
        "fa": "جمنای داره کپشن رو تحلیل می‌کنه...",
        "en": "Gemini is analyzing the caption...",
    },
    "sub_early_audio_fallback": {
        "fa": "کپشن کافی نبود؛ در حال استخراج صدای ویدیو...",
        "en": "Caption had no clues; extracting audio from video...",
    },
    "sub_whisper_transcribing": {
        "fa": "ویسپر داره صدا رو به متن تبدیل می‌کنه...",
        "en": "Whisper is transcribing the audio...",
    },
    "sub_queries_from_transcript": {
        "fa": "ساخت کوئری جستجو از روی متن صدای ویدیو...",
        "en": "Building search queries from the audio transcript...",
    },
    "sub_searching_youtube": {
        "fa": "در حال جستجوی کاندیداها در یوتیوب...",
        "en": "Searching YouTube for candidate videos...",
    },
    "sub_extracting_40s_audio": {
        "fa": "در حال استخراج ۴۰ ثانیه اول صدای ویدیو...",
        "en": "Extracting the first 40 seconds of audio...",
    },
    "sub_matching_candidates": {
        "fa": "در حال تطبیق زیرنویس با {count} کاندیدای برتر...",
        "en": "Matching transcript against top {count} candidates...",
    },
    "sub_ranking_ready": {
        "fa": "رتبه‌بندی نهایی آماده شد!",
        "en": "Final ranking is ready!",
    },
    "sub_openrouter_switch": {
        "fa": "جمنای در دسترس نبود، با مدل پشتیبان (OpenRouter) ادامه می‌دم...",
        "en": "Gemini was unavailable; continuing with backup model (OpenRouter)...",
    },
    "animator_preparing": {
        "fa": "در حال آماده‌سازی...",
        "en": "Preparing...",
    },
    "animator_status_label": {
        "fa": "وضعیت",
        "en": "Status",
    },
    "queue_position": {
        "fa": "تو صف هستی، نفر {pos} ام",
        "en": "You are in the queue, position #{pos}",
    },

    # ─── Search Errors & Warnings ───
    "err_insufficient_input": {
        "fa": (
            "⚠️ <b>ورودی کافی نیست!</b>\n"
            "لطفاً یک پست ویدیویی از کانال‌های تلگرام رو به همراه کپشن برام فوروارد کن."
        ),
        "en": (
            "⚠️ <b>Not enough input!</b>\n"
            "Please forward a video post from a Telegram channel along with its caption."
        ),
    },
    "err_all_models_failed": {
        "fa": "❌ <b>هیچ‌کدوم از مدل‌ها جواب ندادن، چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "❌ <b>All AI models failed to respond. Please try again in a few minutes.</b>",
    },
    "err_caption_failed_send_video": {
        "fa": "❌ <b>تحلیل متن به نتیجه نرسید؛ اگه فایل ویدیو رو داری، خود ویدیو رو با صداش بفرست.</b>",
        "en": "❌ <b>Caption analysis found no clues. If you have the video file, please forward the video with its audio.</b>",
    },
    "err_no_clues_found": {
        "fa": "❌ <b>سرنخ کافی توی کپشن یا صدای ویدیو پیدا نشد.</b>",
        "en": "❌ <b>Could not extract enough clues from the caption or video audio.</b>",
    },
    "err_no_youtube_results": {
        "fa": (
            "😕 <b>هیچ ویدیوی مشابهی در یوتیوب پیدا نشد.</b>\n"
            "ممکنه منبع اصلی این کلیپ توی شبکه X (توییتر) یا TikTok باشه."
        ),
        "en": (
            "😕 <b>No matching videos found on YouTube.</b>\n"
            "The original clip might be from X (Twitter), TikTok, or Instagram."
        ),
    },
    "err_unhandled_search": {
        "fa": "⚠️ <b>خطایی موقع پردازش پیش اومد، چند لحظه دیگه دوباره امتحان کن.</b>",
        "en": "⚠️ <b>An unexpected error occurred while processing your request. Please try again shortly.</b>",
    },
    "warn_caption_timeout": {
        "fa": "تحلیل کپشن بیش از حد طول کشید (تایم‌اوت شد).",
        "en": "Caption analysis timed out.",
    },
    "warn_audio_extract_timeout": {
        "fa": "استخراج صدای ویدیو تایم‌اوت شد.",
        "en": "Audio extraction timed out.",
    },
    "warn_whisper_timeout": {
        "fa": "تبدیل صدا به متن (Whisper) تایم‌اوت شد.",
        "en": "Audio transcription (Whisper) timed out.",
    },
    "warn_match_timeout": {
        "fa": "تطبیق زیرنویس یوتیوب تایم‌اوت شد؛ رتبه‌بندی بر اساس اطلاعات کپشنه.",
        "en": "YouTube transcript matching timed out; ranked by caption metadata.",
    },
    "audio_verify_fallback_notice": {
        "fa": "تطبیق صوتی به خاطر محدودیت دسترسی یوتیوب انجام نشد و رتبه‌بندی بر اساس اطلاعات کپشنه.",
        "en": "Audio verification was skipped due to YouTube access limits; ranked by caption metadata.",
    },

    # ─── Search Result Formatting & Inline Buttons ───
    "res_header_audio_verified": {
        "fa": "✅ <b>منبع اصلی ویدیو با تایید صوتی پیدا شد!</b>",
        "en": "✅ <b>Original video source found (Audio Verified)!</b>",
    },
    "res_header_metadata_match": {
        "fa": "✅ <b>بهترین منبع منطبق در یوتیوب پیدا شد!</b>",
        "en": "✅ <b>Best matching YouTube source found!</b>",
    },
    "res_top_choice": {
        "fa": "🏆 <b>انتخاب اول: {title}</b>",
        "en": "🏆 <b>Top Match: {title}</b>",
    },
    "res_channel": {
        "fa": "👤 <b>کانال یوتیوب:</b> <code>{channel}</code>",
        "en": "👤 <b>YouTube Channel:</b> <code>{channel}</code>",
    },
    "res_duration": {
        "fa": "⏱ <b>مدت زمان کل:</b> <code>{duration}</code>",
        "en": "⏱ <b>Total Duration:</b> <code>{duration}</code>",
    },
    "res_confidence": {
        "fa": "🎯 <b>اطمینان نهایی:</b> <b>{pct}%</b>",
        "en": "🎯 <b>Confidence:</b> <b>{pct}%</b>",
    },
    "res_timestamp": {
        "fa": "📍 <b>نقطه شروع کلیپ شما در یوتیوب:</b> از دقیقه <code>{ts}</code> (ثانیه {sec})",
        "en": "📍 <b>Clip Start Timestamp on YouTube:</b> at <code>{ts}</code> ({sec}s)",
    },
    "res_match_reasons": {
        "fa": "💡 <b>دلایل تطابق:</b> <i>{reasons}</i>",
        "en": "💡 <b>Match Reasons:</b> <i>{reasons}</i>",
    },
    "res_matched_quote": {
        "fa": "🗣 <b>بخشی از دیالوگ تطبیق‌یافته:</b>\n<blockquote>«{snippet}»</blockquote>",
        "en": "🗣 <b>Matched Transcript Snippet:</b>\n<blockquote>\"{snippet}\"</blockquote>",
    },
    "res_direct_link_block": {
        "fa": "\n🔗 <b>لینک مستقیم تماشا:</b>\n{url}\n🖼 <a href='{thumb}'>مشاهده تصویر کاور (Thumbnail)</a>",
        "en": "\n🔗 <b>Direct Watch Link:</b>\n{url}\n🖼 <a href='{thumb}'>View Video Thumbnail</a>",
    },
    "res_candidates_header": {
        "fa": "🗂 <b>کارت‌های مقایسه ۳ کاندیدای برتر:</b>\n",
        "en": "🗂 <b>Top 3 Candidates Comparison:</b>\n",
    },
    "res_cand_ts_badge": {
        "fa": " | 📍 دقیقه <code>{ts}</code>",
        "en": " | 📍 At <code>{ts}</code>",
    },
    "res_cand_audio_badge": {
        "fa": " | 🎙 صدا: <code>{pct}%</code>",
        "en": " | 🎙 Audio: <code>{pct}%</code>",
    },
    "res_cand_card": {
        "fa": (
            "{medal} <b>{title}</b>\n"
            "   📺 کانال: <code>{channel}</code>\n"
            "   ⏱ زمان: <code>{duration}</code> | 📊 اطمینان: <b>{pct}%</b>{audio_badge}{ts_badge}\n"
            "   🔗 <a href='{url}'>لینک ویدیو</a>  •  "
            "🖼 <a href='{thumb}'>پیش‌نمایش تامبنیل</a>\n"
        ),
        "en": (
            "{medal} <b>{title}</b>\n"
            "   📺 Channel: <code>{channel}</code>\n"
            "   ⏱ Duration: <code>{duration}</code> | 📊 Confidence: <b>{pct}%</b>{audio_badge}{ts_badge}\n"
            "   🔗 <a href='{url}'>Video Link</a>  •  "
            "🖼 <a href='{thumb}'>Thumbnail Preview</a>\n"
        ),
    },
    "res_entities": {
        "fa": "🧩 <b>موجودیت‌ها:</b> <code>{entities}</code>",
        "en": "🧩 <b>Extracted Entities:</b> <code>{entities}</code>",
    },
    "res_dubbed_tag": {
        "fa": "{lang} (ترجمه‌شده از دوبله)",
        "en": "{lang} (translated from dubbed audio)",
    },
    "res_detected_audio_lang": {
        "fa": "🎙 <b>زبان صدای تشخیص‌داده‌شده:</b> <code>{lang_tag}</code>",
        "en": "🎙 <b>Detected Audio Language:</b> <code>{lang_tag}</code>",
    },
    "res_control_panel_hint": {
        "fa": "\n🎛 <i>از دکمه‌های زیر برای دانلود زیرنویس، سافت‌ساب یا خلاصه استفاده کن:</i>",
        "en": "\n🎛 <i>Use the buttons below for Persian subtitles, softsub video, or smart summary:</i>",
    },
    "openrouter_footer": {
        "fa": "\n<i>🤖 این نتیجه با مدل پشتیبان ساخته شد (جمنای در دسترس نبود)</i> <code>{model}</code>",
        "en": "\n<i>🤖 Generated by backup model (Gemini was unavailable)</i> <code>{model}</code>",
    },
    "approximate_timing_notice": {
        "fa": "⏱ <i>زمان‌بندی این زیرنویس تقریبیه (متن از روی صدا ساخته شده)</i>",
        "en": "⏱ <i>Subtitle timing is approximate (transcribed from audio)</i>",
    },

    # ─── Control Panel Button Labels ───
    "btn_watch_at_ts": {
        "fa": "▶️ تماشا از دقیقه {ts} در یوتیوب",
        "en": "▶️ Watch from {ts} on YouTube",
    },
    "btn_watch_main": {
        "fa": "▶️ تماشای ویدیوی اصلی در یوتیوب",
        "en": "▶️ Watch Original Video on YouTube",
    },
    "share_text_template": {
        "fa": "🎬 منبع اصلی ویدیو در یوتیوب:\n{title}\n🔗 {url}",
        "en": "🎬 Original video on YouTube:\n{title}\n🔗 {url}",
    },
    "btn_dl_srt_fa": {
        "fa": "📥 دانلود زیرنویس فارسی (.SRT)",
        "en": "📥 Persian Subtitles (.SRT)",
    },
    "btn_dl_softsub": {
        "fa": "🎬 دانلود ویدیو با سافت‌ساب (CC فارسی)",
        "en": "🎬 Video with Persian Softsub (CC)",
    },
    "btn_link_sub": {
        "fa": "🔗 لینک سافت‌ساب",
        "en": "🔗 Softsub Link",
    },
    "btn_link_orig": {
        "fa": "🔗 لینک اصلی",
        "en": "🔗 Original Link",
    },
    "btn_link_audio": {
        "fa": "🎧 لینک صدا",
        "en": "🎧 Audio Link",
    },
    "btn_smart_summary": {
        "fa": "📝 نکات کلیدی و خلاصه هوشمند",
        "en": "📝 Smart Summary & Chapters",
    },
    "btn_dl_srt_bi": {
        "fa": "🔄 زیرنویس دوزبانه (FA+EN)",
        "en": "🔄 Bilingual Subtitles (FA+EN)",
    },
    "btn_web_player": {
        "fa": "🌐 تماشای آنلاین با زیرنویس در وب",
        "en": "🌐 Watch Online (Persian Web Player)",
    },
    "btn_share": {
        "fa": "📤 اشتراک‌گذاری",
        "en": "📤 Share",
    },
    "btn_thumb_n": {
        "fa": "🖼 کاور گزینه {idx}",
        "en": "🖼 Thumbnail #{idx}",
    },

    # ─── Callback Actions: SRT, Softsub, Summary, Web Player ───
    "srt_mode_bi": {
        "fa": "زیرنویس دوزبانه (فارسی + انگلیسی)",
        "en": "Bilingual Subtitles (Persian + English)",
    },
    "srt_mode_fa": {
        "fa": "زیرنویس فارسی (.SRT)",
        "en": "Persian Subtitles (.SRT)",
    },
    "cb_preparing_item": {
        "fa": "⏳ در حال آماده‌سازی {item}...",
        "en": "⏳ Preparing {item}...",
    },
    "msg_preparing_item": {
        "fa": "⏳ <b>در حال آماده‌سازی {item}...</b>",
        "en": "⏳ <b>Preparing {item}...</b>",
    },
    "pipe_srt": {
        "fa": "دریافت تایم‌کدها -> ترجمه هوشمند -> ساخت فایل SRT",
        "en": "Fetch timecodes -> AI translation -> Build SRT file",
    },
    "srt_ready_caption": {
        "fa": (
            "✅ <b>فایل {mode_title} آماده شد!</b>\n\n"
            "🎬 <b>عنوان:</b> {title}\n"
            "👤 <b>کانال:</b> <code>{channel}</code>\n"
            "📊 <b>تعداد خطوط زیرنویس:</b> <code>{count} خط</code>\n\n"
            "👁‍🗨 <b>پیش‌نمایش ۳ دیالوگ اول:</b>\n"
            "<blockquote>{preview}</blockquote>"
        ),
        "en": (
            "✅ <b>{mode_title} file is ready!</b>\n\n"
            "🎬 <b>Title:</b> {title}\n"
            "👤 <b>Channel:</b> <code>{channel}</code>\n"
            "📊 <b>Subtitle Lines:</b> <code>{count} lines</code>\n\n"
            "👁‍🗨 <b>First 3 Lines Preview:</b>\n"
            "<blockquote>{preview}</blockquote>"
        ),
    },
    "err_srt_timeout": {
        "fa": "⏱ <b>زمان ترجمه زیرنویس بیش از حد مجاز (۹۰ ثانیه) طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "⏱ <b>Subtitle translation timed out (90s limit). Please try again in a few minutes.</b>",
    },
    "cb_starting_softsub": {
        "fa": "🎬 فرآیند ساخت ویدیوی سافت‌ساب شروع شد...",
        "en": "🎬 Starting softsub video generation...",
    },
    "msg_preparing_softsub": {
        "fa": "⏳ <b>در حال آماده‌سازی ویدیوی سافت‌ساب...</b>",
        "en": "⏳ <b>Preparing softsubbed video...</b>",
    },
    "pipe_softsub": {
        "fa": "ترجمه زیرنویس -> دریافت ویدیو -> الصاق سافت‌ساب",
        "en": "Translate subtitles -> Download video -> Mux softsub tracks",
    },
    "softsub_uploading": {
        "fa": "در حال آپلود ویدیوی سافت‌ساب در تلگرام ({size_mb:.1f} MB)...",
        "en": "Uploading softsubbed video to Telegram ({size_mb:.1f} MB)...",
    },
    "softsub_ready_caption": {
        "fa": (
            "🎬 <b>{title}</b>\n"
            "👤 <b>کانال:</b> <code>{channel}</code>\n"
            "📦 <b>حجم فایل:</b> <code>{size_mb:.1f} MB</code>\n\n"
            "✨ <b>ویژگی سافت‌ساب (CC):</b>\n"
            "زیرنویس فارسی و انگلیسی داخل ویدیو قرار گرفته؛ توی پلیر تلگرام روی دکمه <b>CC</b> بزن تا فعال بشه."
        ),
        "en": (
            "🎬 <b>{title}</b>\n"
            "👤 <b>Channel:</b> <code>{channel}</code>\n"
            "📦 <b>File Size:</b> <code>{size_mb:.1f} MB</code>\n\n"
            "✨ <b>Softsub (CC) Tracks Included:</b>\n"
            "Both Persian and English subtitle tracks are embedded inside the video. Tap the <b>CC</b> button in the Telegram video player to toggle them."
        ),
    },
    "err_softsub_timeout": {
        "fa": "⏱ <b>ساخت ویدیوی سافت‌ساب بیش از حد مجاز (۱۸۰ ثانیه) طول کشید. می‌تونی از دکمه‌ی دانلود زیرنویس (.SRT) استفاده کنی.</b>",
        "en": "⏱ <b>Softsub video generation timed out (180s limit). You can still use the Persian Subtitles (.SRT) button.</b>",
    },
    "err_softsub_too_large_tg": {
        "fa": "⚠️ <b>حجم ویدیو بیشتر از سقف ۵۰ مگابایت تلگرامه. می‌تونی فایل زیرنویس فارسی (.SRT) رو از دکمه‌ی بالا دانلود کنی.</b>",
        "en": "⚠️ <b>The video exceeds Telegram's 50 MB upload limit. You can download the Persian Subtitles (.SRT) file using the button above.</b>",
    },
    "err_softsub_generic": {
        "fa": "❌ <b>ساخت ویدیوی سافت‌ساب با خطا مواجه شد، چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "❌ <b>Failed to build the softsubbed video. Please try again in a few minutes.</b>",
    },
    "cb_starting_summary": {
        "fa": "📝 در حال تحلیل ویدیو و استخراج نکات کلیدی...",
        "en": "📝 Analyzing video content and extracting key takeaways...",
    },
    "msg_preparing_summary": {
        "fa": "⏳ <b>در حال تحلیل هوشمند محتوای ویدیو...</b>",
        "en": "⏳ <b>Analyzing video content with AI...</b>",
    },
    "pipe_summary": {
        "fa": "استخراج متن -> تحلیل محتوا -> تدوین فصل‌ها",
        "en": "Extract transcript -> AI content analysis -> Build chapters",
    },
    "summary_header": {
        "fa": (
            "📝 <b>خلاصه هوشمند و نکات کلیدی ویدیو</b>\n"
            "🎬 <b>{title}</b>\n"
            "👤 کانال: <code>{channel}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🎯 <b>تیتر کلیدی:</b>\n<b>{headline}</b>\n\n"
            "💡 <b>خلاصه مدیریتی:</b>\n<blockquote>{tldr}</blockquote>\n\n"
            "🔑 <b>مهم‌ترین نکات ویدیو:</b>"
        ),
        "en": (
            "📝 <b>Smart Video Summary & Key Takeaways (Persian)</b>\n"
            "🎬 <b>{title}</b>\n"
            "👤 Channel: <code>{channel}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🎯 <b>Headline:</b>\n<b>{headline}</b>\n\n"
            "💡 <b>Executive Summary (TL;DR):</b>\n<blockquote>{tldr}</blockquote>\n\n"
            "🔑 <b>Key Takeaways:</b>"
        ),
    },
    "summary_tech_terms_header": {
        "fa": "\n🧪 <b>اصطلاحات و مدل‌های تخصصی:</b>",
        "en": "\n🧪 <b>Technical Terms & Models:</b>",
    },
    "summary_chapters_header": {
        "fa": "\n⏱ <b>فصل‌بندی زمانی ویدیو:</b>",
        "en": "\n⏱ <b>Timestamped Video Chapters:</b>",
    },
    "err_summary_timeout": {
        "fa": "⏱ <b>تولید خلاصه بیش از حد مجاز (۹۰ ثانیه) طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "⏱ <b>Summary generation timed out (90s limit). Please try again in a few minutes.</b>",
    },
    "cb_starting_web_player": {
        "fa": "🌐 در حال ساخت پلیر وب تعاملی با زیرنویس فارسی...",
        "en": "🌐 Building interactive web player with Persian subtitles...",
    },
    "msg_preparing_web_player": {
        "fa": "⏳ <b>در حال ساخت پلیر وب تعاملی...</b>",
        "en": "⏳ <b>Building interactive HTML5 web player...</b>",
    },
    "pipe_web_player": {
        "fa": "آماده‌سازی زیرنویس فارسی -> ساخت پلیر HTML5",
        "en": "Prepare Persian subtitles -> Build HTML5 player",
    },
    "btn_open_yt_cc": {
        "fa": "▶️ باز کردن مستقیم در یوتیوب (CC فعال)",
        "en": "▶️ Open Directly on YouTube (CC Enabled)",
    },
    "web_player_ready_caption": {
        "fa": (
            "🌐 <b>پلیر وب تعاملی دوزبانه آماده شد!</b>\n\n"
            "🎬 <b>{title}</b>\n\n"
            "✨ <b>نحوه استفاده:</b>\n"
            "فایل <code>.html</code> بالا رو توی مرورگر گوشی یا کامپیوتر باز کن تا ویدیو با زیرنویس زنده فارسی و لیست دیالوگ‌های قابل کلیک پخش بشه."
        ),
        "en": (
            "🌐 <b>Bilingual Interactive Web Player is Ready!</b>\n\n"
            "🎬 <b>{title}</b>\n\n"
            "✨ <b>How to Use:</b>\n"
            "Open the <code>.html</code> file above in your phone or desktop browser to watch the video with live Persian subtitles and a clickable bilingual transcript."
        ),
    },
    "err_web_player_timeout": {
        "fa": "⏱ <b>ساخت پلیر وب بیش از حد مجاز (۹۰ ثانیه) طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "⏱ <b>Web player generation timed out (90s limit). Please try again in a few minutes.</b>",
    },
    "web_player_live_placeholder": {
        "fa": "▶️ ویدیو را پخش کنید تا زیرنویس فارسی نمایش داده شود...",
        "en": "▶️ Play the video to display live Persian subtitles...",
    },

    # ─── Direct Streaming Links (handlers/link_handler.py & services/media_links.py) ───
    "link_label_sub": {
        "fa": "لینک مستقیم ویدیو با سافت‌ساب فارسی",
        "en": "Direct Video Link with Persian Softsub",
    },
    "link_pipe_sub": {
        "fa": "ترجمه زیرنویس -> دانلود ویدیو -> الصاق سافت‌ساب -> ساخت لینک",
        "en": "Translate subtitles -> Download video -> Mux softsub -> Create link",
    },
    "link_label_orig": {
        "fa": "لینک مستقیم ویدیوی اصلی (بدون زیرنویس)",
        "en": "Direct Original Video Link (No Subtitles)",
    },
    "link_pipe_orig": {
        "fa": "دریافت استریم 720p از یوتیوب -> آماده‌سازی لینک مستقیم",
        "en": "Download 720p stream from YouTube -> Create direct link",
    },
    "link_label_audio": {
        "fa": "لینک مستقیم فایل صوتی (M4A)",
        "en": "Direct Audio Link (M4A)",
    },
    "link_pipe_audio": {
        "fa": "دریافت صدای باکیفیت از یوتیوب -> آماده‌سازی لینک مستقیم",
        "en": "Download high-quality audio from YouTube -> Create direct link",
    },
    "link_not_configured_alert": {
        "fa": "قابلیت لینک مستقیم روی این سرور تنظیم نشده است.",
        "en": "Direct streaming links are not configured on this server.",
    },
    "ttl_hours_mins": {
        "fa": "{hours} ساعت و {mins} دقیقه",
        "en": "{hours}h {mins}m",
    },
    "ttl_hours_only": {
        "fa": "{hours} ساعت",
        "en": "{hours} hours",
    },
    "ttl_mins_only": {
        "fa": "{mins} دقیقه",
        "en": "{mins} minutes",
    },
    "link_ready_message": {
        "fa": (
            "🔗 <b>{label} آماده شد!</b>\n\n"
            "🎬 <b>عنوان:</b> {title}\n"
            "👤 <b>کانال:</b> <code>{channel}</code>\n"
            "📦 <b>حجم فایل:</b> <code>{size_mb} MB</code>\n"
            "⏳ <b>اعتبار لینک:</b> تا <code>{ttl}</code> آینده\n\n"
            "📋 <b>لینک مستقیم (کپی کن):</b>\n"
            "<code>{url}</code>\n\n"
            "📱 <b>نحوه تماشا / دانلود:</b>\n"
            "می‌تونی لینک بالا رو توی مرورگر بزنی تا دانلود بشه، یا توی برنامه <b>VLC</b> / <b>MX Player</b> قسمت <i>Open Network Stream</i> وارد کنی تا مستقیم و بدون دانلود کامل پخش بشه (پشتیبانی از جلو/عقب بردن و انتخاب زیرنویس فارسی/انگلیسی).\n\n"
            "⚠️ <i>توجه: هر کسی که این لینک رو داشته باشه تا قبل از منقضی شدن می‌تونه فایل رو دانلود کنه.</i>"
        ),
        "en": (
            "🔗 <b>{label} is Ready!</b>\n\n"
            "🎬 <b>Title:</b> {title}\n"
            "👤 <b>Channel:</b> <code>{channel}</code>\n"
            "📦 <b>File Size:</b> <code>{size_mb} MB</code>\n"
            "⏳ <b>Expires in:</b> <code>{ttl}</code>\n\n"
            "📋 <b>Direct Streaming URL (Copy):</b>\n"
            "<code>{url}</code>\n\n"
            "📱 <b>How to Watch / Download:</b>\n"
            "Open the link in your browser to download, or paste it into <b>VLC</b> / <b>MX Player</b> under <i>Open Network Stream</i> for instant playback with seeking and Persian/English subtitle track selection.\n\n"
            "⚠️ <i>Note: Anyone with this link can download the file until it expires.</i>"
        ),
    },
    "err_link_timeout": {
        "fa": "⏱ <b>زمان آماده‌سازی لینک مستقیم بیش از حد مجاز طول کشید. چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "⏱ <b>Direct link generation timed out. Please try again in a few minutes.</b>",
    },
    "err_link_generic": {
        "fa": "❌ <b>ساخت لینک مستقیم با خطا مواجه شد، چند دقیقه دیگه دوباره امتحان کن.</b>",
        "en": "❌ <b>Failed to generate the direct streaming link. Please try again in a few minutes.</b>",
    },
    "job_err_link_too_long": {
        "fa": "مدت زمان این ویدیو بیشتر از سقف مجاز لینک مستقیم ({max_mins} دقیقه) هست.",
        "en": "This video exceeds the maximum duration allowed for direct links ({max_mins} minutes).",
    },
    "media_err_download_failed": {
        "fa": "فایل رسانه از یوتیوب دانلود نشد (احتمالاً حجم بیش از حد مجاز یا محدودیت دسترسی).",
        "en": "Failed to download media from YouTube (file size limit exceeded or access restricted).",
    },
    "link_err_not_enabled": {
        "fa": "سرویس لینک مستقیم روی این سرور فعال نشده است.",
        "en": "Direct streaming link service is not enabled on this server.",
    },
    "link_prog_sub_done_downloading": {
        "fa": "ترجمه زیرنویس کامل شد -> در حال دانلود ویدیو (720p)...",
        "en": "Subtitle translation complete -> Downloading video (720p)...",
    },
    "link_prog_muxing": {
        "fa": "در حال الصاق زیرنویس فارسی و انگلیسی (Zero-Encoding Mux)...",
        "en": "Muxing Persian and English subtitle tracks (Zero-Encoding Mux)...",
    },
    "link_err_mux_failed": {
        "fa": "خطا در الصاق زیرنویس به فایل ویدیو.",
        "en": "Failed to mux subtitle tracks into the video container.",
    },
    "link_prog_downloading_orig": {
        "fa": "در حال دانلود ویدیوی اصلی (تا کیفیت 720p)...",
        "en": "Downloading original video (up to 720p)...",
    },
    "link_prog_downloading_audio": {
        "fa": "در حال استخراج و دانلود فایل صوتی باکیفیت (M4A)...",
        "en": "Downloading high-quality audio track (M4A)...",
    },
    "link_prog_registering": {
        "fa": "در حال ساخت لینک مستقیم استریم...",
        "en": "Registering file and generating signed streaming URL...",
    },
    "link_prog_done": {
        "fa": "لینک مستقیم آماده شد!",
        "en": "Direct streaming link is ready!",
    },

    # ─── SubtitleService Progress & Errors ───
    "sub_prog_cached": {
        "fa": "زیرنویس ترجمه‌شده از کش بارگذاری شد!",
        "en": "Translated subtitles loaded from cache!",
    },
    "sub_prog_fetching": {
        "fa": "در حال دریافت زیرنویس و تایم‌کدهای ویدیو...",
        "en": "Fetching video transcript and timecodes...",
    },
    "sub_prog_translating_batch": {
        "fa": "در حال ترجمه هوشمند زیرنویس... (دسته {current} از {total})",
        "en": "Translating subtitles to Persian... (batch {current} of {total})",
    },
    "sub_prog_starting_batches": {
        "fa": "شروع ترجمه {lines} خط دیالوگ در {batches} دسته...",
        "en": "Translating {lines} dialogue lines across {batches} batches...",
    },
    "sub_prog_translation_done": {
        "fa": "ترجمه زیرنویس فارسی کامل شد!",
        "en": "Persian subtitle translation completed!",
    },
    "sum_prog_fetching": {
        "fa": "در حال دریافت متن ویدیو...",
        "en": "Fetching full video transcript...",
    },
    "sum_prog_analyzing": {
        "fa": "در حال استخراج نکات کلیدی و فصل‌بندی زمانی...",
        "en": "Extracting key takeaways and timestamped chapters...",
    },
    "sum_prog_done": {
        "fa": "خلاصه هوشمند آماده شد!",
        "en": "Smart summary is ready!",
    },
    "mux_prog_downloading_video": {
        "fa": "ترجمه کامل شد -> در حال دانلود ویدیو از یوتیوب...",
        "en": "Translation complete -> Downloading video from YouTube...",
    },
    "mux_err_download_failed": {
        "fa": "دانلود فایل ویدیو از یوتیوب انجام نشد.",
        "en": "Failed to download the video file from YouTube.",
    },
    "mux_prog_ffmpeg": {
        "fa": "دریافت ویدیو کامل شد -> در حال الصاق سافت‌ساب فارسی (FFmpeg)...",
        "en": "Video downloaded -> Muxing Persian softsub tracks (FFmpeg)...",
    },
    "mux_err_ffmpeg": {
        "fa": "خطا در الصاق ترک زیرنویس با FFmpeg.",
        "en": "FFmpeg error while muxing subtitle tracks.",
    },
    "mux_prog_ready_uploading": {
        "fa": "ویدیوی سافت‌ساب آماده شد! در حال آپلود در تلگرام...",
        "en": "Softsub video ready! Uploading to Telegram...",
    },
    "ffmpeg_sub_title_fa": {
        "fa": "فارسی (AI Softsub)",
        "en": "Persian (AI Softsub)",
    },

    # ─── TranscriptProvider Errors & Progress ───
    "transcript_err_blocked": {
        "fa": "یوتیوب دسترسی سرور رو بلاک کرده و روش جایگزین هم جواب نداد، چند دقیقه دیگه دوباره امتحان کن",
        "en": "YouTube blocked server access and the fallback method also failed. Please try again in a few minutes.",
    },
    "transcript_err_no_captions": {
        "fa": "این ویدئو زیرنویس نداره و نتونستم از روی صدا هم متنش رو دربیارم",
        "en": "This video has no captions and audio transcription also failed.",
    },
    "transcript_err_quota_exceeded": {
        "fa": "سهمیه‌ی روزانه‌ی رونویسی تموم شده، فردا دوباره امتحان کن",
        "en": "Daily video transcription quota has been reached. Please try again tomorrow.",
    },
    "transcript_err_queue_timeout": {
        "fa": "الان سرور شلوغه، چند دقیقه دیگه امتحان کن",
        "en": "The server is currently busy. Please try again in a few minutes.",
    },
    "transcript_err_error": {
        "fa": "خطایی توی دریافت زیرنویس پیش اومد و روش جایگزین هم جواب نداد، چند دقیقه دیگه دوباره امتحان کن",
        "en": "An error occurred while fetching the transcript and the fallback also failed. Please try again later.",
    },
    "tp_cached": {
        "fa": "زیرنویس از کش بارگذاری شد...",
        "en": "Transcript loaded from cache...",
    },
    "tp_fetching_proxy": {
        "fa": "دریافت زیرنویس از طریق پروکسی...",
        "en": "Fetching transcript via proxy...",
    },
    "tp_fetching_direct": {
        "fa": "تلاش مستقیم برای دریافت زیرنویس یوتیوب...",
        "en": "Fetching transcript directly from YouTube...",
    },
    "tp_fetching_supadata": {
        "fa": "استفاده از سرویس پشتیبان زیرنویس (Supadata)...",
        "en": "Using backup caption service (Supadata)...",
    },
    "tp_fallback_blocked": {
        "fa": "یوتیوب IP سرور رو بلاک کرده؛ دارم از روش جایگزین استفاده می‌کنم...",
        "en": "YouTube blocked the server IP; switching to fallback transcription...",
    },
    "tp_fallback_no_captions": {
        "fa": "زیرنویس آماده پیدا نشد؛ دارم از روش جایگزین (رونویسی با جمنای) استفاده می‌کنم...",
        "en": "No ready captions found; transcribing video audio with Gemini...",
    },
    "tp_transcribing_window": {
        "fa": "رونویسی بخش {current} از {total} با جمنای...",
        "en": "Transcribing part {current} of {total} with Gemini...",
    },

    # ─── JobManager Rejections & Admin Alerts ───
    "admin_alert_header": {
        "fa": "🚨 <b>هشدار سلامت سرور:</b>\n{text}",
        "en": "🚨 <b>Server Health Alert:</b>\n{text}",
    },
    "admin_alert_low_disk": {
        "fa": "فضای خالی دیسک به <code>{free_mb} MB</code> رسیده (کمتر از حد مجاز <code>{min_mb} MB</code>).",
        "en": "Free disk space dropped to <code>{free_mb} MB</code> (below minimum <code>{min_mb} MB</code>).",
    },
    "admin_alert_low_disk_stop": {
        "fa": "فضای خالی دیسک به <code>{free_mb} MB</code> رسیده و دانلودهای جدید متوقف شدن.",
        "en": "Free disk space is at <code>{free_mb} MB</code>; new downloads are paused.",
    },
    "admin_alert_proxy_down": {
        "fa": "پروکسی یوتیوب (WARP/HTTP Proxy) در دسترس نیست و اتصال TCP برقرار نشد.",
        "en": "YouTube proxy (WARP/HTTP Proxy) is unreachable (TCP connection failed).",
    },
    "job_err_video_too_long": {
        "fa": "این ویدئو بیشتر از {max_mins} دقیقه‌ست و ساخت سافت‌ساب براش فعال نیست؛ لطفاً از دکمه‌ی دانلود زیرنویس فارسی (.SRT) استفاده کن.",
        "en": "This video is longer than {max_mins} minutes, so softsub muxing is disabled. Please use the Persian Subtitles (.SRT) button instead.",
    },
    "job_err_low_disk": {
        "fa": "فضای خالی سرور موقتا کمه، چند دقیقه دیگه دوباره امتحان کن.",
        "en": "Server disk space is temporarily low. Please try again in a few minutes.",
    },
    "job_err_user_busy": {
        "fa": "الان یه درخواست سنگین در حال پردازش داری، صبر کن تموم بشه بعد دوباره امتحان کن.",
        "en": "You already have a heavy request in progress. Please wait for it to finish first.",
    },
    "job_err_user_rate_limit": {
        "fa": "تعداد درخواست‌هات تو یک ساعت گذشته به سقف مجاز ({limit} بار) رسیده، کمی بعد دوباره امتحان کن.",
        "en": "You have reached the hourly limit ({limit} heavy requests per hour). Please try again later.",
    },
    "job_err_shutting_down": {
        "fa": "ربات داره آپدیت می‌شه، چند ثانیه دیگه دوباره امتحان کن",
        "en": "The bot is updating and restarting. Please try again in a few seconds.",
    },
    "job_err_queue_full": {
        "fa": "الان سرور شلوغه، چند دقیقه دیگه امتحان کن",
        "en": "The server queue is currently full. Please try again in a few minutes.",
    },

    # ─── YouTubeSearcher & AudioVerifier Match Reasons ───
    "duration_unknown": {
        "fa": "نامشخص",
        "en": "Unknown",
    },
    "reason_keywords": {
        "fa": "کلمات کلیدی: {entities}",
        "en": "Keywords: {entities}",
    },
    "reason_exact_duration": {
        "fa": "تطابق دقیق زمانی ({duration})",
        "en": "Exact duration match ({duration})",
    },
    "reason_close_duration": {
        "fa": "زمان نزدیک ({duration})",
        "en": "Close duration ({duration})",
    },
    "reason_audio_match": {
        "fa": "تطابق صدای ویدئو ({pct}%) در دقیقه {ts}",
        "en": "Audio match ({pct}%) at {ts}",
    },

    # ─── /version & /stats (handlers/stats_handler.py) ───
    "version_message": {
        "fa": (
            "🤖 <b>وضعیت و نسخه‌ی ربات:</b>\n\n"
            "• <b>کامیت (Git):</b> <code>{commit}</code>\n"
            "• <b>تاریخ کامیت:</b> <code>{commit_date}</code>\n"
            "• <b>زمان راه‌اندازی:</b> <code>{started_utc}</code>\n"
            "• <b>آپ‌تایم:</b> <code>{uptime}</code>\n"
            "• <b>مدل سریع فعال:</b> <code>{fast_model}</code>\n"
            "• <b>مدارهای باز (Circuit Breaker):</b> <code>{open_circuits}</code>\n"
            "• <b>پشتیبان OpenRouter:</b> <code>{openrouter_enabled}</code>\n"
            "• <b>فضای خالی دیسک:</b> <code>{free_disk_mb} MB</code>\n"
            "• <b>حجم پوشه کاری:</b> <code>{work_dir_size_mb} MB</code>\n"
            "• <b>پردازش‌های فعال / صف:</b> <code>{active_jobs} active / {queue_length} queued</code>\n"
            "• <b>اتصال پروکسی یوتیوب:</b> <code>{proxy_reachable}</code>\n"
            "• <b>نصب بودن Deno:</b> <code>{deno_found}</code>\n"
            "• <b>مصرف امروز Gemini-URL:</b> <code>{gemini_url_today}</code>\n"
            "• <b>مصرف ماهانه Supadata:</b> <code>{supadata_month}</code>"
        ),
        "en": (
            "🤖 <b>Bot Version & Server Status:</b>\n\n"
            "• <b>Git Commit:</b> <code>{commit}</code>\n"
            "• <b>Commit Date:</b> <code>{commit_date}</code>\n"
            "• <b>Started (UTC):</b> <code>{started_utc}</code>\n"
            "• <b>Uptime:</b> <code>{uptime}</code>\n"
            "• <b>Active Fast Model:</b> <code>{fast_model}</code>\n"
            "• <b>Open Circuit Breakers:</b> <code>{open_circuits}</code>\n"
            "• <b>OpenRouter Fallback:</b> <code>{openrouter_enabled}</code>\n"
            "• <b>Free Disk Space:</b> <code>{free_disk_mb} MB</code>\n"
            "• <b>Work Dir Size:</b> <code>{work_dir_size_mb} MB</code>\n"
            "• <b>Active / Queued Jobs:</b> <code>{active_jobs} active / {queue_length} queued</code>\n"
            "• <b>YouTube Proxy Reachable:</b> <code>{proxy_reachable}</code>\n"
            "• <b>Deno Installed:</b> <code>{deno_found}</code>\n"
            "• <b>Gemini-URL Quota Today:</b> <code>{gemini_url_today}</code>\n"
            "• <b>Supadata Quota This Month:</b> <code>{supadata_month}</code>"
        ),
    },
    "stats_header_block": {
        "fa": (
            "📊 <b>گزارش آماری ربات ({days} روز گذشته)</b>\n\n"
            "👥 <b>کاربران یکتا:</b>\n"
            "• امروز: <code>{u_today}</code> | ۷ روز: <code>{u_7d}</code> | کل: <code>{u_all}</code>\n\n"
            "🔍 <b>جستجوها:</b>\n"
            "• کل: <code>{s_total}</code> | موفق: <code>{s_found}</code> (<code>{s_rate}%</code>)\n"
            "• میانگین اطمینان: <code>{s_conf}%</code>\n\n"
            "🎙 <b>تایید صوتی (Whisper):</b>\n"
            "• استفاده شده: <code>{av_used}</code> | رد شده: <code>{av_skipped}</code> | تایم‌اوت: <code>{av_timeout}</code>\n"
            "• زبان صدا: انگلیسی <code>{av_en}</code> | دوبله <code>{av_dub}</code>\n\n"
            "🖥 <b>وضعیت منابع سرور:</b>\n"
            "• دیسک آزاد: <code>{free_mb} MB</code> | پوشه کاری: <code>{work_mb:.2f} MB</code>\n"
            "• کارهای فعال: <code>{active_jobs}</code> | صف: <code>{queue_len}</code>\n"
            "• پروکسی فعال: <code>{proxy_ok}</code> | Deno: <code>{deno_ok}</code>\n\n"
            "📜 <b>منابع زیرنویس و سهمیه‌ها:</b>\n"
            "• مصرف امروز ویدئوی جمنای: <code>{gem_used}s / {gem_limit}s</code>\n"
            "• مصرف ماهانه Supadata: <code>{supa_used} / {supa_limit}</code>"
        ),
        "en": (
            "📊 <b>Bot Usage Analytics (Last {days} Days)</b>\n\n"
            "👥 <b>Unique Users:</b>\n"
            "• Today: <code>{u_today}</code> | 7d: <code>{u_7d}</code> | All-time: <code>{u_all}</code>\n\n"
            "🔍 <b>Searches:</b>\n"
            "• Total: <code>{s_total}</code> | Found: <code>{s_found}</code> (<code>{s_rate}%</code>)\n"
            "• Avg Confidence: <code>{s_conf}%</code>\n\n"
            "🎙 <b>Audio Verification (Whisper):</b>\n"
            "• Used: <code>{av_used}</code> | Skipped: <code>{av_skipped}</code> | Timed Out: <code>{av_timeout}</code>\n"
            "• Audio Lang: English <code>{av_en}</code> | Dubbed <code>{av_dub}</code>\n\n"
            "🖥 <b>Server Resources:</b>\n"
            "• Free Disk: <code>{free_mb} MB</code> | Work Dir: <code>{work_mb:.2f} MB</code>\n"
            "• Active Jobs: <code>{active_jobs}</code> | Queue: <code>{queue_len}</code>\n"
            "• Proxy Reachable: <code>{proxy_ok}</code> | Deno: <code>{deno_ok}</code>\n\n"
            "📜 <b>Transcript Sources & Quotas:</b>\n"
            "• Gemini-URL Video Today: <code>{gem_used}s / {gem_limit}s</code>\n"
            "• Supadata Credits Month: <code>{supa_used} / {supa_limit}</code>"
        ),
    },
    "stats_sec_actions": {
        "fa": "\n🎛 <b>عملکرد دکمه‌ها:</b>",
        "en": "\n🎛 <b>Button Actions:</b>",
    },
    "stats_sec_steps": {
        "fa": "\n⏱ <b>مراحل پایپ‌لاین:</b>",
        "en": "\n⏱ <b>Pipeline Steps:</b>",
    },
    "stats_sec_llm": {
        "fa": (
            "\n🧠 <b>مدل‌های هوش مصنوعی:</b>\n"
            "• کل پاسخ‌ها: <code>{total}</code> | سوییچ‌ها: <code>{switches}</code>\n"
            "• فال‌بک به OpenRouter امروز: <code>{or_today}</code> | شکست کامل: <code>{all_failed}</code>"
        ),
        "en": (
            "\n🧠 <b>LLM Router Stats:</b>\n"
            "• Total Answered: <code>{total}</code> | Switches: <code>{switches}</code>\n"
            "• OpenRouter Fallbacks Today: <code>{or_today}</code> | All Failed: <code>{all_failed}</code>"
        ),
    },
    "stats_sec_errors": {
        "fa": "\n⚠️ <b>خطاها و تایم‌اوت‌ها:</b>",
        "en": "\n⚠️ <b>Errors & Timeouts:</b>",
    },
    "stats_sec_top_users": {
        "fa": "\n🏆 <b>فعال‌ترین کاربران (Hash Prefix):</b>",
        "en": "\n🏆 <b>Top Returning Users (Hash Prefix):</b>",
    },
    "stats_top_user_row": {
        "fa": "• <code>{hash_prefix}</code>: جستجو={searches} | اکشن={actions}",
        "en": "• <code>{hash_prefix}</code>: searches={searches} | actions={actions}",
    },
    "stats_export_caption": {
        "fa": "📁 خروجی کامل آمار ربات (بازه {days} روزه)",
        "en": "📁 Full analytics JSON export ({days}-day window)",
    },
}


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """
    Look up localized string `key` in `lang` (defaults to current context language),
    falling back to 'fa' if the key or language is missing, and format with `kwargs`.
    """
    if lang is None:
        target_lang = get_current_lang()
    else:
        target_lang = lang if lang in ("fa", "en") else "fa"
    entry = STRINGS.get(key)
    if entry is None:
        logger.warning("Missing i18n key: '%s'", key)
        return key

    template = entry.get(target_lang) or entry.get("fa") or entry.get("en") or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception as exc:
            logger.warning(
                "i18n format error for key='%s' lang='%s': %s",
                key,
                target_lang,
                exc,
            )
            return template
    return template
