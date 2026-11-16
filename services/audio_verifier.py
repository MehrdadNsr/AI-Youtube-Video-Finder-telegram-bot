import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from google import genai
from groq import AsyncGroq
from rapidfuzz import fuzz
from youtube_transcript_api import YouTubeTranscriptApi

from services.job_manager import JobManager
from services.llm_router import LLMRouter
from services.i18n import t
from services.transcript_provider import TranscriptProvider
from services.youtube_searcher import YouTubeCandidate

logger = logging.getLogger(__name__)


@dataclass
class AudioTranscriptionResult:
    """Result of transcribing the extracted audio clip from a Telegram video."""

    raw_text: str
    english_search_text: str
    detected_language: str
    is_dubbed_translated: bool = False


@dataclass
class TranscriptMatchResult:
    """Result of matching the spoken clip transcript against a YouTube video transcript."""

    video_id: str
    match_score: float  # 0.0 to 1.0
    start_seconds: int | None = None
    matched_snippet: str | None = None


class AudioVerifier:
    """
    Phase 2 (Tier 2) Audio Verification Service:
    1. Extracts the first 30-45 seconds of audio via FFmpeg (zero-disk stream or auto-cleaned job dir).
    2. Transcribes audio via Groq Whisper (whisper-large-v3-turbo).
    3. Translates non-English/dubbed transcripts to English via LLMRouter.
    4. Fetches candidate YouTube transcripts via TranscriptProvider (Proxy -> Direct -> Supadata).
    5. When all transcript tiers fail and best metadata score < 0.8, uses cheap Gemini-URL matching on top 2 candidates.
    """

    def __init__(
        self,
        groq_api_key: str,
        gemini_api_key: str,
        whisper_model: str = "whisper-large-v3-turbo",
        gemini_model: str = "gemini-3.8-flash",
        clip_duration_sec: int = 40,
        llm_router: LLMRouter | None = None,
        transcript_provider: TranscriptProvider | None = None,
        job_manager: JobManager | None = None,
    ) -> None:
        self.groq_client = AsyncGroq(api_key=groq_api_key)
        self.gemini_client = genai.Client(api_key=gemini_api_key)
        self.whisper_model = whisper_model
        self.gemini_model = gemini_model
        self.clip_duration_sec = clip_duration_sec
        self.llm_router = llm_router
        self.transcript_provider = transcript_provider
        self.job_manager = job_manager
        self.last_fallback_notice_fa: str | None = None

    async def extract_audio_bytes(
        self, bot: Bot, file_id: str, file_size: int | None = None
    ) -> bytes | None:
        """
        Extract the first N seconds of audio in compact 16kHz Mono MP3 format.
        First attempts direct HTTP range streaming to FFmpeg in memory, falling back
        to a managed job directory if the MP4 moov atom is at EOF.
        """
        if file_size and file_size > 20 * 1024 * 1024:
            logger.warning(
                "File size (%d bytes) exceeds 20MB standard Bot API limit. Attempting get_file anyway...",
                file_size,
            )

        try:
            file_info = await bot.get_file(file_id)
        except TelegramBadRequest as e:
            logger.warning("Cannot fetch file from Telegram (likely >20MB): %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error in bot.get_file: %s", e)
            return None

        if not file_info.file_path:
            return None

        file_url = bot.session.api.file_url(bot.token, file_info.file_path)

        audio_bytes = await self._run_ffmpeg_to_memory(file_url)
        if audio_bytes and len(audio_bytes) > 1500:
            logger.info(
                "Successfully streamed & extracted %d KB of audio in-memory.",
                len(audio_bytes) // 1024,
            )
            return audio_bytes

        logger.info("Direct URL stream returned empty; falling back to workdir download...")
        try:
            if self.job_manager is not None:
                with self.job_manager.create_job_dir("audio_extract_") as tmp_dir:
                    local_video_path = os.path.join(tmp_dir, "input_media")
                    await bot.download_file(file_info.file_path, destination=local_video_path)
                    return await self._run_ffmpeg_to_memory(local_video_path)

            import tempfile

            with tempfile.TemporaryDirectory(prefix="vsf_audio_") as tmp_dir:
                local_video_path = os.path.join(tmp_dir, "input_media")
                await bot.download_file(file_info.file_path, destination=local_video_path)
                return await self._run_ffmpeg_to_memory(local_video_path)
        except Exception as e:
            logger.error("Fallback audio extraction failed: %s", e)
            return None

    async def _run_ffmpeg_to_memory(self, input_source: str) -> bytes | None:
        """Run FFmpeg asynchronously with zombie-safe process cleanup and capture MP3 output."""
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-t",
            str(self.clip_duration_sec),
            "-i",
            input_source,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            "-f",
            "mp3",
            "pipe:1",
        ]
        try:
            returncode, stdout, stderr = await JobManager.run_subprocess_safe(
                cmd, timeout=30.0
            )
            if returncode != 0 or not stdout:
                logger.debug(
                    "FFmpeg exited with code %s: %s",
                    returncode,
                    stderr.decode("utf-8", errors="ignore")[:200],
                )
                return None
            return stdout
        except asyncio.TimeoutError:
            logger.warning("FFmpeg timed out while extracting audio.")
            return None
        except FileNotFoundError:
            logger.error("FFmpeg binary is not installed or not in PATH!")
            return None
        except Exception as e:
            logger.error("Error running FFmpeg subprocess: %s", e)
            return None

    async def transcribe_audio(
        self, audio_bytes: bytes
    ) -> AudioTranscriptionResult | None:
        """
        Transcribe audio bytes using Groq Whisper.
        If dubbed/non-English audio is detected, translates it to English via LLMRouter.
        """
        try:
            transcription = await self.groq_client.audio.transcriptions.create(
                file=("clip.mp3", audio_bytes, "audio/mp3"),
                model=self.whisper_model,
                response_format="verbose_json",
                temperature=0.0,
            )

            raw_text = (getattr(transcription, "text", "") or "").strip()
            language = (getattr(transcription, "language", "en") or "en").lower()

            if len(raw_text) < 12:
                logger.info("Extracted speech is too short or music-only.")
                return None

            persian_chars = len(re.findall(r"[\u0600-\u06FF]", raw_text))
            is_non_english = language not in ("en", "english") or persian_chars > 10

            if is_non_english:
                logger.info(
                    "Detected non-English/dubbed audio (lang=%s). Translating to English for transcript matching...",
                    language,
                )
                translated = await self._translate_to_english(raw_text)
                return AudioTranscriptionResult(
                    raw_text=raw_text,
                    english_search_text=translated or raw_text,
                    detected_language=language,
                    is_dubbed_translated=True,
                )

            return AudioTranscriptionResult(
                raw_text=raw_text,
                english_search_text=raw_text,
                detected_language=language,
                is_dubbed_translated=False,
            )

        except Exception as e:
            logger.error("Groq Whisper transcription failed: %s", e)
            return None

    async def _translate_to_english(self, text: str) -> str | None:
        """Translate dubbed Persian transcript back to natural English via LLMRouter."""
        prompt = (
            "Translate the following spoken transcript back into natural spoken English "
            "as a tech YouTuber would say it. Output ONLY the English translation:\n\n"
            f"{text}"
        )
        try:
            if self.llm_router is not None:
                translated, _ = await self.llm_router.generate_text(
                    task="fast",
                    prompt=prompt,
                )
                return translated.strip() if translated else None

            response = await self.gemini_client.aio.models.generate_content(
                model=self.gemini_model,
                contents=prompt,
            )
            return response.text.strip() if response.text else None
        except Exception as e:
            logger.warning("Failed to translate dubbed transcript: %s", e)
            return None

    def _fetch_transcript_sync(self, video_id: str) -> list[dict[str, Any]]:
        """Direct fallback transcript fetcher when TranscriptProvider is not injected."""
        try:
            if hasattr(YouTubeTranscriptApi, "fetch"):
                api = YouTubeTranscriptApi()
                fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
                if hasattr(fetched, "to_raw_data"):
                    return fetched.to_raw_data()
                return [
                    {
                        "text": getattr(item, "text", ""),
                        "start": float(getattr(item, "start", 0.0)),
                        "duration": float(getattr(item, "duration", 0.0)),
                    }
                    for item in fetched
                ]
            return YouTubeTranscriptApi.get_transcript(
                video_id, languages=["en", "en-US", "en-GB"]
            )
        except Exception as e:
            logger.debug("Transcript unavailable for video %s: %s", video_id, e)
            return []

    def _match_transcript_sliding_window(
        self,
        video_id: str,
        segments: list[dict[str, Any]],
        clip_text: str,
        is_dubbed: bool = False,
    ) -> TranscriptMatchResult:
        """Find the best matching transcript window and exact start second using RapidFuzz."""
        if not segments or not clip_text.strip():
            return TranscriptMatchResult(video_id=video_id, match_score=0.0)

        norm_clip = self._normalize_text(clip_text)
        if len(norm_clip) < 10:
            return TranscriptMatchResult(video_id=video_id, match_score=0.0)

        clip_words = norm_clip.split()
        anchor_phrase = " ".join(clip_words[: min(10, len(clip_words))])

        best_score = 0.0
        best_start_sec = 0
        best_snippet = ""

        target_window_sec = self.clip_duration_sec + 12
        num_segments = len(segments)

        for i in range(num_segments):
            win_start = float(segments[i].get("start", 0.0))
            window_texts: list[str] = []

            j = i
            while j < num_segments:
                seg_start = float(segments[j].get("start", 0.0))
                if seg_start - win_start > target_window_sec and len(window_texts) >= 3:
                    break
                seg_text = str(segments[j].get("text", "")).replace("\n", " ").strip()
                if seg_text:
                    window_texts.append(seg_text)
                j += 1

            raw_window_str = " ".join(window_texts)
            norm_window = self._normalize_text(raw_window_str)
            if not norm_window:
                continue

            p_ratio = fuzz.partial_ratio(norm_clip, norm_window)
            tset_ratio = fuzz.token_set_ratio(norm_clip, norm_window)
            tsort_ratio = fuzz.token_sort_ratio(norm_clip, norm_window)

            if is_dubbed:
                combined = (0.60 * tset_ratio + 0.30 * p_ratio + 0.10 * tsort_ratio) / 100.0
            else:
                combined = (0.50 * p_ratio + 0.35 * tset_ratio + 0.15 * tsort_ratio) / 100.0

            if combined > best_score:
                best_score = combined
                best_start_sec = int(win_start)
                best_snippet = raw_window_str[:180] + ("..." if len(raw_window_str) > 180 else "")

                best_anchor_score = 0.0
                for k in range(i, min(j, i + 6)):
                    sub_text = self._normalize_text(
                        " ".join(str(segments[m].get("text", "")) for m in range(k, min(j, k + 3)))
                    )
                    anchor_sim = fuzz.partial_ratio(anchor_phrase, sub_text)
                    if anchor_sim > best_anchor_score and anchor_sim >= 75:
                        best_anchor_score = anchor_sim
                        best_start_sec = int(float(segments[k].get("start", win_start)))

        return TranscriptMatchResult(
            video_id=video_id,
            match_score=round(best_score, 4),
            start_seconds=max(0, best_start_sec),
            matched_snippet=best_snippet,
        )

    async def verify_and_rerank_candidates(
        self,
        candidates: list[YouTubeCandidate],
        transcription: AudioTranscriptionResult,
    ) -> list[YouTubeCandidate]:
        """
        Fetch candidate transcripts via TranscriptProvider (Proxy -> Direct -> Supadata).
        When caption tiers fail for candidates and best metadata confidence < 0.8,
        falls back to cheap Gemini-URL matching on the top 2 candidates.
        """
        self.last_fallback_notice_fa = None
        if not candidates or not transcription.english_search_text:
            return candidates

        if self.transcript_provider is not None:
            for c in candidates:
                self.transcript_provider.set_duration_hint(
                    c.video_id, c.duration_seconds
                )
            fetch_tasks = [
                self.transcript_provider.fetch_caption_tiers_only(cand.video_id)
                for cand in candidates
            ]
            provider_results = await asyncio.gather(
                *fetch_tasks, return_exceptions=True
            )
            transcripts_list: list[list[dict[str, Any]]] = [
                res.segments
                if not isinstance(res, Exception) and hasattr(res, "segments")
                else []
                for res in provider_results
            ]
        else:
            raw_tasks = [
                asyncio.to_thread(self._fetch_transcript_sync, cand.video_id)
                for cand in candidates
            ]
            raw_res = await asyncio.gather(*raw_tasks, return_exceptions=True)
            transcripts_list = [
                r if isinstance(r, list) else [] for r in raw_res
            ]

        match_results_by_id: dict[str, TranscriptMatchResult] = {}
        for cand, segments in zip(candidates, transcripts_list):
            if segments:
                match_results_by_id[cand.video_id] = (
                    self._match_transcript_sliding_window(
                        video_id=cand.video_id,
                        segments=segments,
                        clip_text=transcription.english_search_text,
                        is_dubbed=transcription.is_dubbed_translated,
                    )
                )

        # If caption tiers failed for all candidates, try cheap Gemini-URL matching on top 2 if metadata < 0.8
        if not match_results_by_id and self.transcript_provider is not None:
            best_meta_conf = max((c.metadata_score for c in candidates), default=0.0)
            if best_meta_conf < 0.80:
                gemini_matches = (
                    await self.transcript_provider.match_candidates_with_gemini_url(
                        candidates[:2], transcription.english_search_text
                    )
                )
                for vid_id, (found, start_sec, conf) in gemini_matches.items():
                    match_results_by_id[vid_id] = TranscriptMatchResult(
                        video_id=vid_id,
                        match_score=conf if found else min(0.25, conf),
                        start_seconds=start_sec if found else None,
                        matched_snippet=None,
                    )
                if not match_results_by_id:
                    self.last_fallback_notice_fa = t("audio_verify_fallback_notice")

        any_strong_audio_match = False
        strong_threshold = 0.58 if transcription.is_dubbed_translated else 0.68

        for cand in candidates:
            match_res = match_results_by_id.get(cand.video_id)
            if not match_res:
                continue

            cand.audio_match_score = match_res.match_score

            if match_res.match_score >= strong_threshold:
                any_strong_audio_match = True
                cand.matched_timestamp_seconds = match_res.start_seconds
                cand.matched_transcript_snippet = match_res.matched_snippet

                boosted_score = (0.25 * cand.metadata_score) + (
                    0.75 * match_res.match_score
                )
                cand.confidence_score = round(
                    min(0.99, max(boosted_score, match_res.match_score)), 4
                )

                ts_label = cand.formatted_timestamp or "00:00"
                cand.match_reasons.insert(
                    0,
                    t("reason_audio_match", pct=cand.audio_match_percent, ts=ts_label),
                )
            elif match_res.match_score >= 0.50:
                cand.matched_timestamp_seconds = match_res.start_seconds
                cand.matched_transcript_snippet = match_res.matched_snippet
                cand.confidence_score = round(
                    (0.50 * cand.metadata_score) + (0.50 * match_res.match_score),
                    4,
                )

        if any_strong_audio_match:
            for cand in candidates:
                if (
                    cand.audio_match_score is not None
                    and cand.audio_match_score < 0.48
                ):
                    cand.confidence_score = round(cand.metadata_score * 0.55, 4)

        return sorted(candidates, key=lambda c: c.confidence_score, reverse=True)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Strip bracketed annotations and punctuation for fuzzy matching."""
        cleaned = re.sub(r"\[.*?\]|\(.*?\)", " ", text.lower())
        cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()
