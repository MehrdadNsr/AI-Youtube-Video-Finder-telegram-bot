import asyncio
import hashlib
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
import yt_dlp
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi

try:
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError:
    GenericProxyConfig = None  # type: ignore[assignment,misc]

from config import Settings
from middlewares.analytics_middleware import current_user_id_var
from services.analytics import Analytics
from services.job_manager import JobManager
from services.llm_router import LLMRouter
from services.ytdlp_options import build_opts

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], Awaitable[None]]
TranscriptSource = Literal["proxy", "direct", "supadata", "gemini_url"]


class TranscriptFetchError(RuntimeError):
    """Exception carrying an honest Persian user-facing message based on error_kind."""

    MESSAGES_FA: dict[str, str] = {
        "blocked": "یوتیوب دسترسی سرور رو بلاک کرده و روش جایگزین هم جواب نداد، چند دقیقه دیگه دوباره امتحان کن",
        "no_captions": "این ویدئو زیرنویس نداره و نتونستم از روی صدا هم متنش رو دربیارم",
        "quota_exceeded": "سهمیه‌ی روزانه‌ی رونویسی تموم شده، فردا دوباره امتحان کن",
        "queue_timeout": "الان سرور شلوغه، چند دقیقه دیگه امتحان کن",
        "error": "خطایی توی دریافت زیرنویس پیش اومد و روش جایگزین هم جواب نداد، چند دقیقه دیگه دوباره امتحان کن",
    }

    def __init__(self, error_kind: str, custom_message: str | None = None) -> None:
        self.error_kind = error_kind
        self.user_message_fa = custom_message or self.MESSAGES_FA.get(
            error_kind, self.MESSAGES_FA["error"]
        )
        super().__init__(self.user_message_fa)


@dataclass
class TranscriptResult:
    """Normalized transcript output returned by TranscriptProvider."""

    segments: list[dict[str, Any]]
    source: TranscriptSource
    approximate: bool
    error_kind: str | None = None


class TranscribedSegmentItem(BaseModel):
    start_sec: float = Field(..., description="Start timestamp in seconds")
    end_sec: float = Field(..., description="End timestamp in seconds")
    text: str = Field(..., description="Transcribed English speech for this segment")


class TranscribedWindowSchema(BaseModel):
    segments: list[TranscribedSegmentItem] = Field(default_factory=list)


class GeminiVideoMatchSchema(BaseModel):
    found: bool = Field(
        ...,
        description="True if the provided spoken clip transcript occurs in this YouTube video",
    )
    start_seconds: int | None = Field(
        default=None,
        description="Exact second (integer) in the YouTube video where the spoken clip begins, or null if not found",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 and 1.0 that this video contains the clip",
    )


@dataclass
class CachedClipMatch:
    found: bool
    start_seconds: int | None
    confidence: float
    expires_at_monotonic: float


TRANSCRIBE_WINDOW_SYSTEM_PROMPT = """
You are an accurate speech-to-text transcription engine.
Listen to the specified window of the YouTube video and transcribe all spoken dialogue into English segments.
If the speaker is speaking another language, translate the spoken dialogue into natural English.

RULES:
1. Return `segments` as a chronological list of `{start_sec, end_sec, text}`.
2. Use timestamps in seconds (either absolute from the start of the video or relative to the window start).
3. Segment length should be 3 to 7 seconds per item.
4. Ignore background music or silence. Do not hallucinate text.
"""

VIDEO_MATCH_SYSTEM_PROMPT = """
You are an audio-visual verification engine.
Given a YouTube video and a short English transcript of a 30-45 second clip, determine whether that exact clip is spoken inside this YouTube video.
If it is present, set `found=true`, return the exact `start_seconds` (integer seconds from the beginning of the YouTube video where the clip starts), and set `confidence` between 0.0 and 1.0.
If the video only discusses a similar topic with different wording or a different speaker, set `found=false`, `start_seconds=null`, and `confidence` below 0.3.
"""


class TranscriptProvider:
    """
    4-tier YouTube transcript provider:
    (1) youtube-transcript-api via GenericProxyConfig (when YT_PROXY_HTTP is set)
    (2) youtube-transcript-api direct
    (3) Supadata API native captions (when SUPADATA_API_KEY is set, monthly quota guarded)
    (4) Gemini-URL windowed video transcription (when GEMINI_URL_TRANSCRIBE_ENABLED, daily quota guarded)
    """

    SUPADATA_BASE_URL = "https://api.supadata.ai/v1/transcript"
    POSITIVE_CACHE_MAX = 100
    NEGATIVE_CACHE_TTL_SEC = 600.0
    MATCH_CACHE_TTL_SEC = 86400.0
    PROXY_BREAKER_COOLDOWN_SEC = 300.0
    SUPADATA_COOLDOWN_SEC = 600.0
    WINDOW_TIMEOUT_SEC = 75.0
    TOTAL_TRANSCRIBE_BUDGET_SEC = 180.0

    def __init__(
        self,
        settings: Settings,
        llm_router: LLMRouter,
        analytics: Analytics | None = None,
        job_manager: JobManager | None = None,
    ) -> None:
        self.settings = settings
        self.llm_router = llm_router
        self.analytics = analytics
        self.job_manager = job_manager

        raw_supa_key = (
            settings.SUPADATA_API_KEY.get_secret_value().strip()
            if settings.SUPADATA_API_KEY
            else ""
        )
        self._supadata_api_key: str | None = raw_supa_key if raw_supa_key else None

        self._positive_cache: OrderedDict[str, TranscriptResult] = OrderedDict()
        self._negative_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._match_cache: OrderedDict[tuple[str, str], CachedClipMatch] = OrderedDict()
        self._duration_cache: OrderedDict[str, int] = OrderedDict()

        self._proxy_consecutive_failures = 0
        self._proxy_circuit_open = False
        self._proxy_open_until_monotonic = 0.0

        self._direct_circuit_open = False
        self._direct_open_until_monotonic = 0.0

        self._supadata_circuit_open = False
        self._supadata_open_until_monotonic = 0.0

        self._fallback_gemini_sem = asyncio.Semaphore(
            max(1, int(settings.MAX_CONCURRENT_GEMINI_URL_JOBS))
        )
        self._session: aiohttp.ClientSession | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def has_proxy(self) -> bool:
        return bool(self.settings.YT_PROXY_HTTP)

    def set_duration_hint(self, video_id: str, duration_seconds: int | None) -> None:
        if duration_seconds and duration_seconds > 0:
            self._duration_cache[video_id] = int(duration_seconds)
            self._duration_cache.move_to_end(video_id)
            if len(self._duration_cache) > 200:
                self._duration_cache.popitem(last=False)

    def _spawn_analytics(self, event_type: str, **props: Any) -> None:
        if not self.analytics:
            return
        user_id = current_user_id_var.get()
        try:
            task = asyncio.create_task(
                self.analytics.log_event(user_id, event_type, **props)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:
            pass

    def _is_proxy_circuit_open(self) -> bool:
        if not self._proxy_circuit_open:
            return False
        if time.monotonic() >= self._proxy_open_until_monotonic:
            self._proxy_circuit_open = False
            self._proxy_consecutive_failures = 0
            logger.warning("Transcript proxy circuit breaker CLOSED.")
            return False
        return True

    def _record_proxy_success(self) -> None:
        if self._proxy_consecutive_failures > 0 or self._proxy_circuit_open:
            was_open = self._proxy_circuit_open
            self._proxy_consecutive_failures = 0
            self._proxy_circuit_open = False
            if was_open:
                logger.warning("Transcript proxy circuit breaker CLOSED.")

    def _record_proxy_failure(self, err_kind: str) -> None:
        self._proxy_consecutive_failures += 1
        if self._proxy_consecutive_failures >= 2:
            self._proxy_circuit_open = True
            self._proxy_open_until_monotonic = (
                time.monotonic() + self.PROXY_BREAKER_COOLDOWN_SEC
            )
            logger.warning(
                "Transcript proxy circuit breaker OPEN for %ds after %d consecutive failures (last=%s)",
                int(self.PROXY_BREAKER_COOLDOWN_SEC),
                self._proxy_consecutive_failures,
                err_kind,
            )

    def _is_direct_circuit_open(self) -> bool:
        if not self._direct_circuit_open:
            return False
        if time.monotonic() >= self._direct_open_until_monotonic:
            self._direct_circuit_open = False
            logger.warning("Direct YouTube transcript API block circuit breaker CLOSED.")
            return False
        return True

    def _open_direct_block_circuit(self) -> None:
        cooldown = float(self.settings.YT_BLOCK_COOLDOWN_SEC)
        self._direct_circuit_open = True
        self._direct_open_until_monotonic = time.monotonic() + cooldown
        logger.warning(
            "Direct YouTube transcript API block circuit breaker OPEN for %ds.",
            int(cooldown),
        )

    def _is_supadata_circuit_open(self) -> bool:
        if not self._supadata_circuit_open:
            return False
        if time.monotonic() >= self._supadata_open_until_monotonic:
            self._supadata_circuit_open = False
            logger.warning("Supadata tier circuit breaker CLOSED.")
            return False
        return True

    def _open_supadata_circuit(self, status_code: int) -> None:
        self._supadata_circuit_open = True
        self._supadata_open_until_monotonic = (
            time.monotonic() + self.SUPADATA_COOLDOWN_SEC
        )
        logger.warning(
            "Supadata tier circuit breaker OPEN for 600s due to HTTP %d.",
            status_code,
        )

    @staticmethod
    def classify_yt_exception(exc: Exception) -> str:
        """
        Classify youtube-transcript-api exceptions by class name:
        - RequestBlocked / IpBlocked -> 'blocked'
        - TranscriptsDisabled / NoTranscriptFound / VideoUnavailable -> 'no_captions'
        - anything else -> 'error'
        """
        cls_name = exc.__class__.__name__
        if cls_name in ("RequestBlocked", "IpBlocked"):
            return "blocked"
        if cls_name in (
            "TranscriptsDisabled",
            "NoTranscriptFound",
            "NoTranscriptAvailable",
            "VideoUnavailable",
            "VideoUnplayable",
        ):
            return "no_captions"
        msg_lower = str(exc).lower()
        if (
            "requestblocked" in msg_lower
            or "ipblocked" in msg_lower
            or "blocking requests from your ip" in msg_lower
        ):
            return "blocked"
        if (
            "subtitles are disabled" in msg_lower
            or "no transcripts were found" in msg_lower
        ):
            return "no_captions"
        return "error"

    def _store_positive_cache(self, video_id: str, res: TranscriptResult) -> None:
        self._positive_cache[video_id] = res
        self._positive_cache.move_to_end(video_id)
        if len(self._positive_cache) > self.POSITIVE_CACHE_MAX:
            self._positive_cache.popitem(last=False)
        self._negative_cache.pop(video_id, None)

    def _store_negative_cache(self, video_id: str, err_kind: str) -> None:
        self._negative_cache[video_id] = (time.monotonic(), err_kind)
        self._negative_cache.move_to_end(video_id)
        if len(self._negative_cache) > self.POSITIVE_CACHE_MAX:
            self._negative_cache.popitem(last=False)

    def _fetch_yt_api_sync(self, video_id: str, use_proxy: bool) -> list[dict[str, Any]]:
        http_p = self.settings.YT_PROXY_HTTP if use_proxy else None
        https_p = (
            (self.settings.YT_PROXY_HTTPS or self.settings.YT_PROXY_HTTP)
            if use_proxy
            else None
        )

        if hasattr(YouTubeTranscriptApi, "fetch"):
            proxy_cfg = None
            if use_proxy and (http_p or https_p) and GenericProxyConfig is not None:
                proxy_cfg = GenericProxyConfig(
                    http_url=http_p,
                    https_url=https_p,
                )
            api = (
                YouTubeTranscriptApi(proxy_config=proxy_cfg)
                if proxy_cfg is not None
                else YouTubeTranscriptApi()
            )
            try:
                fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
            except Exception as inner_exc:
                if self.classify_yt_exception(inner_exc) == "blocked":
                    raise
                t_list = api.list(video_id)
                transcript = t_list.find_transcript(["en", "en-US", "en-GB"])
                fetched = transcript.fetch()

            if hasattr(fetched, "to_raw_data"):
                return fetched.to_raw_data()
            return [
                {
                    "text": getattr(item, "text", ""),
                    "start": float(getattr(item, "start", 0.0)),
                    "duration": float(getattr(item, "duration", 3.0)),
                }
                for item in fetched
            ]

        proxies = (
            {"http": http_p, "https": https_p}
            if use_proxy and (http_p or https_p)
            else None
        )
        return YouTubeTranscriptApi.get_transcript(
            video_id, languages=["en", "en-US", "en-GB"], proxies=proxies
        )

    async def _try_yt_tier(
        self, video_id: str, use_proxy: bool
    ) -> TranscriptResult:
        source: TranscriptSource = "proxy" if use_proxy else "direct"
        t0 = time.monotonic()
        try:
            raw_segments = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_yt_api_sync, video_id, use_proxy),
                timeout=20.0,
            )
            dur = round(time.monotonic() - t0, 3)
            if not raw_segments:
                raise ValueError("Empty transcript returned")

            if use_proxy:
                self._record_proxy_success()

            logger.info(
                "Transcript fetch ok | provider=%s video_id=%s segments=%d seconds=%.2f",
                source,
                video_id,
                len(raw_segments),
                dur,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source=source,
                ok=True,
                seconds=dur,
                error_kind=None,
                video_seconds=0,
            )
            res = TranscriptResult(
                segments=raw_segments,
                source=source,
                approximate=False,
                error_kind=None,
            )
            self._store_positive_cache(video_id, res)
            return res

        except Exception as exc:
            dur = round(time.monotonic() - t0, 3)
            err_kind = self.classify_yt_exception(exc)
            logger.warning(
                "Transcript fetch failed | provider=%s video_id=%s seconds=%.2f error_class=%s error_kind=%s",
                source,
                video_id,
                dur,
                exc.__class__.__name__,
                err_kind,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source=source,
                ok=False,
                seconds=dur,
                error_kind=err_kind,
                video_seconds=0,
            )
            if use_proxy:
                self._record_proxy_failure(err_kind)
            elif err_kind == "blocked":
                self._open_direct_block_circuit()

            return TranscriptResult(
                segments=[],
                source=source,
                approximate=False,
                error_kind=err_kind,
            )

    async def _try_supadata_tier(self, video_id: str) -> TranscriptResult:
        """
        Fetch native captions via Supadata API (`mode=native`, `text=false`, `lang=en`).
        Polls every 2s up to 60s if an async jobId is returned.
        Converts millisecond offset/duration to seconds.
        """
        if not self._supadata_api_key or self._is_supadata_circuit_open():
            return TranscriptResult(
                segments=[], source="supadata", approximate=False, error_kind="error"
            )

        if self.analytics:
            used_credits = await self.analytics.get_supadata_credits_this_month()
            if used_credits >= self.settings.SUPADATA_MONTHLY_LIMIT:
                logger.warning(
                    "Supadata monthly credit limit reached (%d/%d); skipping Supadata tier.",
                    used_credits,
                    self.settings.SUPADATA_MONTHLY_LIMIT,
                )
                return TranscriptResult(
                    segments=[],
                    source="supadata",
                    approximate=False,
                    error_kind="quota_exceeded",
                )

        t0 = time.monotonic()
        session = await self._get_http_session()
        headers = {"x-api-key": self._supadata_api_key}
        params = {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "lang": "en",
            "text": "false",
            "mode": "native",
        }

        try:
            req_timeout = aiohttp.ClientTimeout(total=20.0)
            async with session.get(
                self.SUPADATA_BASE_URL,
                headers=headers,
                params=params,
                timeout=req_timeout,
            ) as resp:
                status = resp.status
                if status in (402, 429):
                    self._open_supadata_circuit(status)
                    raise RuntimeError(f"Supadata HTTP {status}")
                if status in (404, 206):
                    raise LookupError("No native captions on Supadata")
                if status not in (200, 202):
                    raise RuntimeError(f"Supadata HTTP {status}")
                data = await resp.json()

            # Handle asynchronous job polling if jobId is returned
            if isinstance(data, dict) and "jobId" in data and "content" not in data:
                job_id = str(data["jobId"]).strip()
                poll_url = f"{self.SUPADATA_BASE_URL}/{job_id}"
                poll_deadline = time.monotonic() + 60.0
                while time.monotonic() < poll_deadline:
                    await asyncio.sleep(2.0)
                    async with session.get(
                        poll_url, headers=headers, timeout=req_timeout
                    ) as poll_resp:
                        if poll_resp.status in (402, 429):
                            self._open_supadata_circuit(poll_resp.status)
                            raise RuntimeError(f"Supadata poll HTTP {poll_resp.status}")
                        if poll_resp.status != 200:
                            continue
                        poll_data = await poll_resp.json()
                        job_status = str(poll_data.get("status", "")).lower()
                        if job_status == "completed" or "content" in poll_data:
                            data = poll_data
                            break
                        if job_status in ("failed", "error"):
                            raise LookupError("Supadata job failed")

            content_list = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content_list, list) or not content_list:
                raise LookupError("Supadata returned empty content")

            segments: list[dict[str, Any]] = []
            for item in content_list:
                if not isinstance(item, dict):
                    continue
                txt = str(item.get("text", "")).strip()
                if not txt:
                    continue
                offset_ms = float(item.get("offset", 0.0))
                dur_ms = float(item.get("duration", 2500.0))
                segments.append(
                    {
                        "text": txt,
                        "start": round(max(0.0, offset_ms / 1000.0), 3),
                        "duration": round(max(0.5, dur_ms / 1000.0), 3),
                    }
                )

            if not segments:
                raise LookupError("Supadata parsed 0 segments")

            dur = round(time.monotonic() - t0, 3)
            if self.analytics:
                await self.analytics.add_supadata_credit(1)

            logger.info(
                "Transcript fetch ok | provider=supadata video_id=%s segments=%d seconds=%.2f",
                video_id,
                len(segments),
                dur,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source="supadata",
                ok=True,
                seconds=dur,
                error_kind=None,
                video_seconds=0,
            )
            res = TranscriptResult(
                segments=segments,
                source="supadata",
                approximate=False,
                error_kind=None,
            )
            self._store_positive_cache(video_id, res)
            return res

        except LookupError as exc:
            dur = round(time.monotonic() - t0, 3)
            logger.warning(
                "Transcript fetch failed | provider=supadata video_id=%s seconds=%.2f error_class=%s error_kind=no_captions",
                video_id,
                dur,
                exc.__class__.__name__,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source="supadata",
                ok=False,
                seconds=dur,
                error_kind="no_captions",
                video_seconds=0,
            )
            return TranscriptResult(
                segments=[],
                source="supadata",
                approximate=False,
                error_kind="no_captions",
            )
        except Exception as exc:
            dur = round(time.monotonic() - t0, 3)
            logger.warning(
                "Transcript fetch failed | provider=supadata video_id=%s seconds=%.2f error_class=%s error_kind=error",
                video_id,
                dur,
                exc.__class__.__name__,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source="supadata",
                ok=False,
                seconds=dur,
                error_kind="error",
                video_seconds=0,
            )
            return TranscriptResult(
                segments=[],
                source="supadata",
                approximate=False,
                error_kind="error",
            )

    async def fetch_caption_tiers_only(self, video_id: str) -> TranscriptResult:
        """
        Run non-generative caption tiers in order:
        (1) Proxy (if configured and breaker closed)
        (2) Direct (if breaker closed)
        (3) Supadata (if configured and breaker closed)
        Used by AudioVerifier before falling back to cheap 2-candidate Gemini-URL matching.
        """
        if video_id in self._positive_cache:
            cached = self._positive_cache[video_id]
            self._positive_cache.move_to_end(video_id)
            return cached

        primary_err_kind: str | None = None

        # Tier (1): Proxy
        if self.has_proxy and not self._is_proxy_circuit_open():
            res = await self._try_yt_tier(video_id, use_proxy=True)
            if res.segments:
                return res
            primary_err_kind = res.error_kind

        # Tier (2): Direct
        if not self._is_direct_circuit_open():
            res = await self._try_yt_tier(video_id, use_proxy=False)
            if res.segments:
                return res
            if res.error_kind == "no_captions" or primary_err_kind is None:
                primary_err_kind = res.error_kind
            elif res.error_kind == "blocked":
                primary_err_kind = "blocked"
        elif primary_err_kind is None:
            primary_err_kind = "blocked"

        # Tier (3): Supadata
        if self._supadata_api_key and not self._is_supadata_circuit_open():
            self._spawn_analytics(
                "transcript_fallback_used",
                reason=primary_err_kind or "error",
                mode="supadata",
            )
            supa_res = await self._try_supadata_tier(video_id)
            if supa_res.segments:
                return supa_res
            if supa_res.error_kind == "no_captions" and primary_err_kind != "blocked":
                primary_err_kind = "no_captions"

        return TranscriptResult(
            segments=[],
            source="proxy" if self.has_proxy else "direct",
            approximate=False,
            error_kind=primary_err_kind or "error",
        )

    async def _resolve_video_duration(
        self, video_id: str, duration_hint_sec: int | None = None
    ) -> int:
        if duration_hint_sec and duration_hint_sec > 0:
            self.set_duration_hint(video_id, duration_hint_sec)
            return int(duration_hint_sec)
        if video_id in self._duration_cache:
            return self._duration_cache[video_id]

        def _probe_sync() -> int:
            opts = build_opts("metadata")
            url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return int(info.get("duration") or 0)
            except Exception:
                return 0

        try:
            dur = await asyncio.wait_for(asyncio.to_thread(_probe_sync), timeout=15.0)
        except Exception:
            dur = 0

        if dur > 0:
            self.set_duration_hint(video_id, dur)
            return dur
        return 600

    async def get_segments(
        self,
        video_id: str,
        progress_cb: ProgressCallback | None = None,
        duration_hint_sec: int | None = None,
    ) -> TranscriptResult:
        """
        Execute the full 4-tier transcript chain:
        (1) Proxy -> (2) Direct -> (3) Supadata -> (4) Gemini-URL Transcription.
        """
        if duration_hint_sec and duration_hint_sec > 0:
            self.set_duration_hint(video_id, duration_hint_sec)

        if video_id in self._positive_cache:
            res = self._positive_cache[video_id]
            self._positive_cache.move_to_end(video_id)
            if progress_cb:
                await progress_cb(20, "زیرنویس از کش بارگذاری شد...")
            return res

        neg = self._negative_cache.get(video_id)
        if neg is not None:
            neg_ts, neg_kind = neg
            if time.monotonic() - neg_ts < self.NEGATIVE_CACHE_TTL_SEC:
                logger.info(
                    "Returning negative-cached failure (%s) for video %s",
                    neg_kind,
                    video_id,
                )
                return TranscriptResult(
                    segments=[],
                    source="gemini_url",
                    approximate=True,
                    error_kind=neg_kind,
                )
            self._negative_cache.pop(video_id, None)

        primary_err_kind: str | None = None

        # Tier (1): Proxy
        if self.has_proxy and not self._is_proxy_circuit_open():
            if progress_cb:
                await progress_cb(10, "دریافت زیرنویس از طریق پروکسی...")
            res = await self._try_yt_tier(video_id, use_proxy=True)
            if res.segments:
                return res
            primary_err_kind = res.error_kind

        # Tier (2): Direct
        if not self._is_direct_circuit_open():
            if progress_cb:
                await progress_cb(12, "تلاش مستقیم برای دریافت زیرنویس یوتیوب...")
            res = await self._try_yt_tier(video_id, use_proxy=False)
            if res.segments:
                return res
            if res.error_kind == "no_captions" or primary_err_kind is None:
                primary_err_kind = res.error_kind
            elif res.error_kind == "blocked":
                primary_err_kind = "blocked"
        elif primary_err_kind is None:
            primary_err_kind = "blocked"

        # Tier (3): Supadata
        if self._supadata_api_key and not self._is_supadata_circuit_open():
            if progress_cb:
                await progress_cb(
                    14,
                    "استفاده از سرویس پشتیبان زیرنویس (Supadata)...",
                )
            self._spawn_analytics(
                "transcript_fallback_used",
                reason=primary_err_kind or "error",
                mode="supadata",
            )
            supa_res = await self._try_supadata_tier(video_id)
            if supa_res.segments:
                return supa_res
            if supa_res.error_kind == "no_captions" and primary_err_kind != "blocked":
                primary_err_kind = "no_captions"

        honest_error_kind = primary_err_kind or "error"

        # Tier (4): Gemini URL Transcription
        if not self.settings.GEMINI_URL_TRANSCRIBE_ENABLED:
            self._store_negative_cache(video_id, honest_error_kind)
            return TranscriptResult(
                segments=[],
                source="gemini_url",
                approximate=True,
                error_kind=honest_error_kind,
            )

        if progress_cb:
            if honest_error_kind == "blocked":
                await progress_cb(
                    15,
                    "یوتیوب IP سرور رو بلاک کرده؛ دارم از روش جایگزین استفاده می‌کنم...",
                )
            else:
                await progress_cb(
                    15,
                    "زیرنویس آماده پیدا نشد؛ دارم از روش جایگزین (رونویسی با جمنای) استفاده می‌کنم...",
                )

        self._spawn_analytics(
            "transcript_fallback_used",
            reason=honest_error_kind,
            mode="gemini_url",
        )

        async def _on_queue_pos(pos: int) -> None:
            if progress_cb:
                await progress_cb(15, f"تو صف هستی، نفر {pos} ام")

        if self.job_manager is not None:
            try:
                async with self.job_manager.acquire_slot(
                    "gemini_url", on_queue_position=_on_queue_pos
                ):
                    return await self._execute_gemini_url_tier(
                        video_id=video_id,
                        duration_hint_sec=duration_hint_sec,
                        honest_error_kind=honest_error_kind,
                        progress_cb=progress_cb,
                    )
            except Exception as q_exc:
                if q_exc.__class__.__name__ == "JobRejectedError":
                    return TranscriptResult(
                        segments=[],
                        source="gemini_url",
                        approximate=True,
                        error_kind="queue_timeout",
                    )
                raise

        async with self._fallback_gemini_sem:
            return await self._execute_gemini_url_tier(
                video_id=video_id,
                duration_hint_sec=duration_hint_sec,
                honest_error_kind=honest_error_kind,
                progress_cb=progress_cb,
            )

    async def _execute_gemini_url_tier(
        self,
        video_id: str,
        duration_hint_sec: int | None,
        honest_error_kind: str,
        progress_cb: ProgressCallback | None,
    ) -> TranscriptResult:
        if video_id in self._positive_cache:
            res = self._positive_cache[video_id]
            self._positive_cache.move_to_end(video_id)
            return res

        video_duration = await self._resolve_video_duration(
            video_id, duration_hint_sec
        )

        used_today = (
            await self.analytics.get_gemini_url_seconds_today()
            if self.analytics
            else 0
        )
        if used_today + video_duration > self.settings.GEMINI_URL_DAILY_LIMIT_SEC:
            logger.warning(
                "Gemini-URL daily quota exceeded (%ds used + %ds requested > %ds limit)",
                used_today,
                video_duration,
                self.settings.GEMINI_URL_DAILY_LIMIT_SEC,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source="gemini_url",
                ok=False,
                seconds=0.0,
                error_kind="quota_exceeded",
                video_seconds=0,
            )
            return TranscriptResult(
                segments=[],
                source="gemini_url",
                approximate=True,
                error_kind="quota_exceeded",
            )

        fallback_res = await self._transcribe_via_gemini_url(
            video_id=video_id,
            video_duration=video_duration,
            honest_error_kind=honest_error_kind,
            progress_cb=progress_cb,
        )
        if fallback_res.segments:
            self._store_positive_cache(video_id, fallback_res)
        elif fallback_res.error_kind != "quota_exceeded":
            self._store_negative_cache(
                video_id, fallback_res.error_kind or honest_error_kind
            )
        return fallback_res

    async def _transcribe_via_gemini_url(
        self,
        video_id: str,
        video_duration: int,
        honest_error_kind: str,
        progress_cb: ProgressCallback | None,
    ) -> TranscriptResult:
        t0 = time.monotonic()
        window_sec = max(60, int(self.settings.GEMINI_URL_WINDOW_SEC))
        num_windows = max(1, math.ceil(video_duration / window_sec))
        windows: list[tuple[int, int, int]] = []
        for idx in range(num_windows):
            w_start = idx * window_sec
            w_end = min(video_duration, (idx + 1) * window_sec)
            if w_end > w_start:
                windows.append((idx + 1, w_start, w_end))

        total_windows = len(windows)
        parallel_sem = asyncio.Semaphore(
            max(1, int(self.settings.GEMINI_URL_PARALLEL_WINDOWS))
        )
        yt_url = f"https://www.youtube.com/watch?v={video_id}"

        window_results: dict[int, list[TranscribedSegmentItem]] = {}
        completed_windows = 0
        consumed_video_seconds = 0

        async def _run_window(win_idx: int, w_start: int, w_end: int) -> None:
            nonlocal completed_windows, consumed_video_seconds
            async with parallel_sem:
                if time.monotonic() - t0 >= self.TOTAL_TRANSCRIBE_BUDGET_SEC:
                    logger.warning(
                        "Skipping window %d/%d (%ds..%ds) for %s: 180s total budget spent",
                        win_idx,
                        total_windows,
                        w_start,
                        w_end,
                        video_id,
                    )
                    return

                if progress_cb:
                    pct = 16 + int(((win_idx - 1) / max(1, total_windows)) * 60)
                    await progress_cb(
                        pct,
                        f"رونویسی بخش {win_idx} از {total_windows} با جمنای...",
                    )

                prompt = (
                    f"Transcribe the spoken audio of this video between {w_start}s and {w_end}s "
                    f"into chronological English segments with start_sec and end_sec."
                )
                win_len = max(1, w_end - w_start)
                consumed_video_seconds += win_len
                if self.analytics:
                    await self.analytics.add_gemini_url_seconds(win_len)

                try:
                    parsed, _ = await self.llm_router.generate_json_with_video(
                        url=yt_url,
                        start_s=w_start,
                        end_s=w_end,
                        system=TRANSCRIBE_WINDOW_SYSTEM_PROMPT,
                        prompt=prompt,
                        schema=TranscribedWindowSchema,
                        timeout=self.WINDOW_TIMEOUT_SEC,
                    )
                    window_results[win_idx] = parsed.segments
                except Exception as exc:
                    logger.warning(
                        "Gemini-URL transcription gap on window %d/%d (%ds..%ds) for %s: %s",
                        win_idx,
                        total_windows,
                        w_start,
                        w_end,
                        video_id,
                        exc.__class__.__name__,
                    )
                finally:
                    completed_windows += 1
                    if progress_cb:
                        pct = 16 + int((completed_windows / max(1, total_windows)) * 65)
                        await progress_cb(
                            pct,
                            f"رونویسی بخش {completed_windows} از {total_windows} با جمنای...",
                        )

        try:
            await asyncio.wait_for(
                asyncio.gather(*(_run_window(i, s, e) for i, s, e in windows)),
                timeout=self.TOTAL_TRANSCRIBE_BUDGET_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Gemini-URL transcription hit 180s total budget for %s (completed %d/%d windows)",
                video_id,
                completed_windows,
                total_windows,
            )

        repaired_segments = self._validate_and_repair_windows(
            windows=windows,
            window_results=window_results,
        )
        dur = round(time.monotonic() - t0, 3)

        if not repaired_segments:
            logger.error(
                "Gemini-URL transcription produced 0 segments for %s in %.2fs (initial error_kind=%s)",
                video_id,
                dur,
                honest_error_kind,
            )
            self._spawn_analytics(
                "transcript_fetch",
                source="gemini_url",
                ok=False,
                seconds=dur,
                error_kind=honest_error_kind,
                video_seconds=consumed_video_seconds,
            )
            return TranscriptResult(
                segments=[],
                source="gemini_url",
                approximate=True,
                error_kind=honest_error_kind,
            )

        logger.info(
            "Transcript fetch ok | provider=gemini_url video_id=%s segments=%d windows=%d/%d seconds=%.2f",
            video_id,
            len(repaired_segments),
            len(window_results),
            total_windows,
            dur,
        )
        self._spawn_analytics(
            "transcript_fetch",
            source="gemini_url",
            ok=True,
            seconds=dur,
            error_kind=None,
            video_seconds=consumed_video_seconds,
        )
        return TranscriptResult(
            segments=repaired_segments,
            source="gemini_url",
            approximate=True,
            error_kind=None,
        )

    @staticmethod
    def _validate_and_repair_windows(
        windows: list[tuple[int, int, int]],
        window_results: dict[int, list[TranscribedSegmentItem]],
    ) -> list[dict[str, Any]]:
        """
        Validate and repair Gemini-URL window outputs:
        - Shift relative window timestamps to absolute video timestamps when needed
        - Clamp timestamps to window bounds
        - Enforce monotonic start times
        - Drop empty or duplicated lines
        - Merge overlaps between windows
        """
        raw_repaired: list[dict[str, Any]] = []

        for win_idx, w_start, w_end in windows:
            items = window_results.get(win_idx)
            if not items:
                continue

            win_len = float(w_end - w_start)
            max_start = max((it.start_sec for it in items), default=0.0)
            is_relative = w_start > 0 and max_start <= (win_len + 5.0)

            last_win_start = float(w_start)
            for item in items:
                text = " ".join(str(item.text or "").split()).strip()
                if not text:
                    continue

                s = float(item.start_sec)
                e = float(item.end_sec)
                if is_relative:
                    s += w_start
                    e += w_start

                s = max(float(w_start), min(float(w_end) - 0.5, s))
                if s < last_win_start:
                    s = last_win_start
                last_win_start = s

                if e <= s:
                    e = min(float(w_end), s + 3.0)
                else:
                    e = min(float(w_end), e)

                dur = max(0.5, round(e - s, 3))

                if (
                    raw_repaired
                    and raw_repaired[-1]["text"].lower() == text.lower()
                ):
                    continue

                raw_repaired.append(
                    {
                        "text": text,
                        "start": round(s, 3),
                        "duration": dur,
                    }
                )

        if not raw_repaired:
            return []

        raw_repaired.sort(key=lambda x: x["start"])
        final_segments: list[dict[str, Any]] = []

        for seg in raw_repaired:
            if final_segments:
                prev = final_segments[-1]
                prev_end = prev["start"] + prev["duration"]
                if seg["start"] < prev["start"]:
                    seg["start"] = prev["start"]
                if prev_end > seg["start"]:
                    if seg["start"] - prev["start"] < 0.4:
                        prev["text"] = f"{prev['text']} {seg['text']}"
                        prev["duration"] = round(
                            max(
                                prev["duration"],
                                (seg["start"] + seg["duration"]) - prev["start"],
                            ),
                            3,
                        )
                        continue
                    prev["duration"] = round(
                        max(0.5, seg["start"] - prev["start"]), 3
                    )

            final_segments.append(seg)

        return final_segments

    # ─────────────────────────────────────────────────────────────
    # Cheap Gemini-URL Clip Matching for AudioVerifier
    # ─────────────────────────────────────────────────────────────

    def _get_cached_match(
        self, video_id: str, whisper_hash: str
    ) -> CachedClipMatch | None:
        key = (video_id, whisper_hash)
        cached = self._match_cache.get(key)
        if not cached:
            return None
        if time.monotonic() >= cached.expires_at_monotonic:
            self._match_cache.pop(key, None)
            return None
        self._match_cache.move_to_end(key)
        return cached

    def _set_cached_match(
        self,
        video_id: str,
        whisper_hash: str,
        found: bool,
        start_seconds: int | None,
        confidence: float,
    ) -> None:
        key = (video_id, whisper_hash)
        self._match_cache[key] = CachedClipMatch(
            found=found,
            start_seconds=start_seconds,
            confidence=confidence,
            expires_at_monotonic=time.monotonic() + self.MATCH_CACHE_TTL_SEC,
        )
        self._match_cache.move_to_end(key)
        if len(self._match_cache) > 200:
            self._match_cache.popitem(last=False)

    async def match_candidates_with_gemini_url(
        self,
        candidates: list[Any],
        whisper_text: str,
    ) -> dict[str, tuple[bool, int | None, float]]:
        """
        Verify up to 2 candidates in parallel via a single Gemini-URL call per candidate
        (media_resolution LOW, 60s timeout, global semaphore, 24h cache, daily quota guard).
        Returns {video_id: (found, start_seconds, confidence)}.
        """
        top_two = candidates[:2]
        if not top_two or not whisper_text.strip():
            return {}

        norm_whisper = " ".join(whisper_text.lower().split())
        whisper_hash = hashlib.sha256(norm_whisper.encode("utf-8")).hexdigest()[:16]

        results: dict[str, tuple[bool, int | None, float]] = {}
        uncached_candidates: list[Any] = []

        for cand in top_two:
            cached = self._get_cached_match(cand.video_id, whisper_hash)
            if cached is not None:
                logger.info(
                    "Gemini-URL clip match cache hit for video %s (found=%s, start=%s, conf=%.2f)",
                    cand.video_id,
                    cached.found,
                    cached.start_seconds,
                    cached.confidence,
                )
                results[cand.video_id] = (
                    cached.found,
                    cached.start_seconds,
                    cached.confidence,
                )
            else:
                uncached_candidates.append(cand)

        if not uncached_candidates or not self.settings.GEMINI_URL_TRANSCRIBE_ENABLED:
            return results

        self._spawn_analytics(
            "transcript_fallback_used",
            reason="blocked",
            mode="match_top2",
        )

        async def _run_batch() -> dict[str, tuple[bool, int | None, float]]:
            used_today = (
                await self.analytics.get_gemini_url_seconds_today()
                if self.analytics
                else 0
            )
            est_total_sec = sum(
                min(int(getattr(c, "duration_seconds", 0) or 600), 1800)
                for c in uncached_candidates
            )
            if used_today + est_total_sec > self.settings.GEMINI_URL_DAILY_LIMIT_SEC:
                logger.warning(
                    "Skipping Gemini-URL candidate matching: daily video quota would be exceeded (%ds + %ds > %ds)",
                    used_today,
                    est_total_sec,
                    self.settings.GEMINI_URL_DAILY_LIMIT_SEC,
                )
                return results

            async def _match_one(cand: Any) -> None:
                vid_id = cand.video_id
                cand_dur = int(getattr(cand, "duration_seconds", 0) or 600)
                end_cap = min(cand_dur, 1800)
                yt_url = f"https://www.youtube.com/watch?v={vid_id}"
                prompt = (
                    "Check if the following spoken clip transcript occurs in this YouTube video, "
                    "and if so, return the exact start_seconds where it begins:\n\n"
                    f"CLIP TRANSCRIPT:\n{whisper_text[:1500]}"
                )

                if self.analytics:
                    await self.analytics.add_gemini_url_seconds(end_cap)

                t0 = time.monotonic()
                try:
                    parsed, _ = await asyncio.wait_for(
                        self.llm_router.generate_json_with_video(
                            url=yt_url,
                            start_s=0,
                            end_s=end_cap,
                            system=VIDEO_MATCH_SYSTEM_PROMPT,
                            prompt=prompt,
                            schema=GeminiVideoMatchSchema,
                            timeout=60.0,
                        ),
                        timeout=60.0,
                    )
                    dur = round(time.monotonic() - t0, 3)
                    self._set_cached_match(
                        vid_id,
                        whisper_hash,
                        parsed.found,
                        parsed.start_seconds,
                        parsed.confidence,
                    )
                    results[vid_id] = (
                        parsed.found,
                        parsed.start_seconds,
                        parsed.confidence,
                    )
                    self._spawn_analytics(
                        "transcript_fetch",
                        source="gemini_url",
                        ok=True,
                        seconds=dur,
                        error_kind=None,
                        video_seconds=end_cap,
                    )
                except Exception as exc:
                    dur = round(time.monotonic() - t0, 3)
                    logger.warning(
                        "Gemini-URL clip match failed for %s in %.2fs (%s): %s",
                        vid_id,
                        dur,
                        exc.__class__.__name__,
                        exc,
                    )
                    self._spawn_analytics(
                        "transcript_fetch",
                        source="gemini_url",
                        ok=False,
                        seconds=dur,
                        error_kind="error",
                        video_seconds=end_cap,
                    )

            await asyncio.gather(*(_match_one(c) for c in uncached_candidates))
            return results

        if self.job_manager is not None:
            try:
                async with self.job_manager.acquire_slot("gemini_url"):
                    return await _run_batch()
            except JobRejectedError:
                return results

        async with self._fallback_gemini_sem:
            return await _run_batch()
