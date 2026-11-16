import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import settings
from handlers.link_handler import link_router
from handlers.stats_handler import get_version_info, stats_router
from handlers.video_handler import router as video_router
from middlewares.analytics_middleware import AnalyticsMiddleware
from services.analytics import Analytics
from services.audio_verifier import AudioVerifier
from services.gemini_extractor import GeminiExtractor
from services.job_manager import JobManager
from services.link_server import LinkServer
from services.llm_router import LLMRouter
from services.media_links import MediaLinkService
from services.subtitle_service import SubtitleService
from services.transcript_provider import TranscriptProvider
from services.youtube_searcher import YouTubeSearcher


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Initializing Video Source Finder Bot...")

    gemini_api_key = settings.GEMINI_API_KEY.get_secret_value()

    # 1. Privacy-preserving SQLite analytics & quota store
    analytics = Analytics(
        db_path=settings.ANALYTICS_DB_PATH,
        salt=settings.ANALYTICS_SALT.get_secret_value(),
        confidence_threshold=settings.CONFIDENCE_THRESHOLD,
        gemini_url_daily_limit_sec=settings.GEMINI_URL_DAILY_LIMIT_SEC,
        supadata_monthly_limit=settings.SUPADATA_MONTHLY_LIMIT,
    )
    await analytics.ensure_initialized()

    # 2. Resilient multi-provider LLM router with startup catalog validation
    llm_router = LLMRouter(
        settings=settings,
        analytics=analytics,
    )
    await llm_router.start_validation_loop()

    # 3. Resource safety & concurrency manager
    job_manager = JobManager(
        settings=settings,
        analytics=analytics,
    )

    # 4. 4-tier TranscriptProvider (Proxy -> Direct -> Supadata -> Gemini-URL)
    transcript_provider = TranscriptProvider(
        settings=settings,
        llm_router=llm_router,
        analytics=analytics,
        job_manager=job_manager,
    )

    # 5. Core domain services
    gemini_extractor = GeminiExtractor(
        api_key=gemini_api_key,
        model_name=settings.GEMINI_MODEL,
        llm_router=llm_router,
    )
    youtube_searcher = YouTubeSearcher(results_per_query=5)
    audio_verifier = AudioVerifier(
        groq_api_key=settings.GROQ_API_KEY.get_secret_value(),
        gemini_api_key=gemini_api_key,
        whisper_model=settings.WHISPER_MODEL,
        gemini_model=settings.GEMINI_MODEL,
        clip_duration_sec=settings.AUDIO_CLIP_DURATION_SEC,
        llm_router=llm_router,
        transcript_provider=transcript_provider,
        job_manager=job_manager,
    )
    subtitle_service = SubtitleService(
        gemini_api_key=gemini_api_key,
        primary_model=settings.GEMINI_MODEL,
        batch_size=40,
        max_concurrent_batches=3,
        llm_router=llm_router,
        transcript_provider=transcript_provider,
        job_manager=job_manager,
    )

    # 6. Direct Streaming Link Server & MediaLinkService
    link_server = LinkServer(settings=settings)
    await link_server.start()
    media_link_service = MediaLinkService(
        settings=settings,
        subtitle_service=subtitle_service,
        link_server=link_server,
        job_manager=job_manager,
    )

    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await job_manager.start_background_tasks(bot)

    v_info = await get_version_info(llm_router, job_manager, analytics)
    logger.info(
        "Bot Version | commit=%s date='%s' started='%s' fast_model=%s openrouter_enabled=%s link_server=%s free_disk=%sMB proxy_ok=%s deno=%s",
        v_info["commit"],
        v_info["commit_date"],
        v_info["started_utc"],
        v_info["fast_model"],
        v_info["openrouter_enabled"],
        "yes" if link_server.is_enabled else "no",
        v_info["free_disk_mb"],
        v_info["proxy_reachable"],
        v_info["deno_found"],
    )

    dp = Dispatcher()
    dp.update.outer_middleware(AnalyticsMiddleware(analytics=analytics))

    # Register stats_router and link_router BEFORE video_router (since video_router matches F.text)
    dp.include_router(stats_router)
    dp.include_router(link_router)
    dp.include_router(video_router)

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        logger.info("Bot is polling and ready...")
        await dp.start_polling(
            bot,
            gemini_extractor=gemini_extractor,
            youtube_searcher=youtube_searcher,
            audio_verifier=audio_verifier,
            subtitle_service=subtitle_service,
            analytics=analytics,
            llm_router=llm_router,
            job_manager=job_manager,
            transcript_provider=transcript_provider,
            link_server=link_server,
            media_link_service=media_link_service,
        )
    finally:
        await job_manager.shutdown()
        await link_server.stop()
        await transcript_provider.close()
        await llm_router.close()
        await bot.session.close()
        logger.info("Bot session and all background resources closed gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
