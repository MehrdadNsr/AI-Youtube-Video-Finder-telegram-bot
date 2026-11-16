import asyncio
import html
import json
import logging
import os
import re
import shutil
import urllib.request
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import yt_dlp
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from services.job_manager import JobManager
from services.llm_router import LLMRouter
from services.i18n import t
from services.transcript_provider import TranscriptFetchError, TranscriptProvider
from services.ytdlp_options import build_opts

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], Awaitable[None]]


@dataclass
class SubtitleSegment:
    """Single subtitle line with start/end timestamps, original text, and Persian translation."""

    index: int
    start_sec: float
    end_sec: float
    en_text: str
    fa_text: str = ""

    @staticmethod
    def format_srt_timestamp(seconds: float) -> str:
        """Format float seconds as SubRip timestamp (HH:MM:SS,mmm)."""
        total_ms = max(0, int(round(seconds * 1000)))
        hours, rem = divmod(total_ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        secs, ms = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    @staticmethod
    def format_short_time(seconds: float) -> str:
        """Format seconds as MM:SS or HH:MM:SS for previews."""
        total_sec = max(0, int(seconds))
        hours, rem = divmod(total_sec, 3600)
        minutes, secs = divmod(rem, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"


class TranslatedLineSchema(BaseModel):
    index: int = Field(..., description="Exact index of the subtitle segment")
    fa_text: str = Field(..., description="Fluent Persian translation of the subtitle line")


class TranslatedBatchSchema(BaseModel):
    items: list[TranslatedLineSchema]


class VideoChapterSchema(BaseModel):
    timestamp_seconds: int = Field(..., description="Start time of this section in seconds")
    title_fa: str = Field(..., description="Short, catchy Persian title for this section")
    summary_fa: str = Field(..., description="1-2 sentence Persian explanation of this section")


class VideoSummarySchema(BaseModel):
    headline_fa: str = Field(..., description="One-line compelling Persian headline for the video")
    tldr_fa: str = Field(..., description="2-3 sentence executive summary in fluent Persian")
    key_takeaways_fa: list[str] = Field(
        ..., description="4 to 6 actionable bullet points and key insights in Persian"
    )
    technical_terms: list[str] = Field(
        default_factory=list,
        description="Important technical terms/models mentioned with brief Persian definition",
    )
    chapters: list[VideoChapterSchema] = Field(
        default_factory=list,
        description="Chronological chapters/sections of the video with exact start seconds",
    )


@dataclass
class VideoMetadataInfo:
    video_id: str
    title: str
    channel: str
    duration_seconds: int
    thumbnail_url: str


@dataclass
class SoftsubMuxResult:
    """Output of downloading and muxing softsub tracks into an MP4 container."""

    muxed_video_path: str
    srt_fa_path: str
    thumbnail_path: str | None
    title: str
    channel: str
    duration_seconds: int
    width: int
    height: int
    file_size_bytes: int
    work_dir_path: str
    approximate_timing: bool = False

    def cleanup(self) -> None:
        """Remove temporary working directory after Telegram upload finishes."""
        try:
            if self.work_dir_path and os.path.exists(self.work_dir_path):
                shutil.rmtree(self.work_dir_path, ignore_errors=True)
        except Exception as e:
            logger.debug("Error cleaning up softsub work dir: %s", e)


TRANSLATION_SYSTEM_PROMPT = """
You are an expert technical translator specializing in AI, Software Engineering, and Tech YouTube videos.
Translate the provided JSON array of English subtitle segments into natural, fluent, and concise Persian (فارسی) suitable for video subtitles.

CRITICAL RULES:
1. Maintain the EXACT `index` for every single item. Do not skip or merge indices.
2. Keep technical proper nouns, model names, programming terms, and frameworks in English (e.g., 'Claude', 'Prompt', 'Agent', 'API', 'Fine-tuning', 'LLM').
3. Keep each Persian subtitle concise so the viewer can read it comfortably within the segment duration.
4. Use natural spoken Persian matching the tone of tech creators.
"""

SUMMARY_SYSTEM_PROMPT = """
You are an elite AI & Tech content analyst. Given the timestamped transcript of a YouTube video, 
generate a comprehensive, structured Persian summary (`VideoSummarySchema`).
- Write in clear, natural Persian.
- Extract 4-6 high-value key takeaways.
- Break the video down into logical chronological chapters with their exact `timestamp_seconds`.
"""


class SubtitleService:
    """
    Subtitle translation engine, smart video summarizer, and zero-encoding FFmpeg softsub muxer.
    Fetches transcripts through TranscriptProvider and routes LLM calls through LLMRouter.
    """

    def __init__(
        self,
        gemini_api_key: str,
        primary_model: str = "gemini-3.8-flash",
        batch_size: int = 40,
        max_concurrent_batches: int = 3,
        llm_router: LLMRouter | None = None,
        transcript_provider: TranscriptProvider | None = None,
        job_manager: JobManager | None = None,
    ) -> None:
        self.client = genai.Client(api_key=gemini_api_key)
        self.primary_model = primary_model
        self.batch_size = batch_size
        self.max_concurrent_batches = max_concurrent_batches
        self.llm_router = llm_router
        self.transcript_provider = transcript_provider
        self.job_manager = job_manager

        self._segments_cache: OrderedDict[str, list[SubtitleSegment]] = OrderedDict()
        self._approximate_cache: OrderedDict[str, bool] = OrderedDict()
        self._summary_cache: OrderedDict[str, VideoSummarySchema] = OrderedDict()
        self._meta_cache: OrderedDict[str, VideoMetadataInfo] = OrderedDict()
        self._max_cache_items = 50

    def is_approximate(self, video_id: str) -> bool:
        """Return True if the transcript for `video_id` was generated with approximate timing."""
        return bool(self._approximate_cache.get(video_id, False))

    def cache_video_metadata(
        self,
        video_id: str,
        title: str,
        channel: str,
        duration_seconds: int | None,
        thumbnail_url: str = "",
    ) -> None:
        """Cache video metadata discovered during search for instant access in button callbacks."""
        self._meta_cache[video_id] = VideoMetadataInfo(
            video_id=video_id,
            title=title,
            channel=channel,
            duration_seconds=duration_seconds or 0,
            thumbnail_url=thumbnail_url or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        )
        self._meta_cache.move_to_end(video_id)
        if len(self._meta_cache) > self._max_cache_items:
            self._meta_cache.popitem(last=False)
        if self.transcript_provider is not None and duration_seconds:
            self.transcript_provider.set_duration_hint(video_id, duration_seconds)

    async def get_video_metadata(self, video_id: str) -> VideoMetadataInfo:
        """Return cached video metadata or fetch it via yt-dlp using build_opts('metadata')."""
        if video_id in self._meta_cache:
            return self._meta_cache[video_id]

        def _extract() -> VideoMetadataInfo:
            opts = build_opts("metadata")
            url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return VideoMetadataInfo(
                        video_id=video_id,
                        title=str(info.get("title") or f"YouTube_{video_id}"),
                        channel=str(info.get("channel") or info.get("uploader") or "YouTube"),
                        duration_seconds=int(info.get("duration") or 0),
                        thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    )
            except Exception as e:
                logger.warning("Metadata fetch failed for %s: %s", video_id, e)
                return VideoMetadataInfo(
                    video_id=video_id,
                    title=f"YouTube_Video_{video_id}",
                    channel="YouTube",
                    duration_seconds=0,
                    thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                )

        meta = await asyncio.to_thread(_extract)
        self.cache_video_metadata(
            video_id=meta.video_id,
            title=meta.title,
            channel=meta.channel,
            duration_seconds=meta.duration_seconds,
            thumbnail_url=meta.thumbnail_url,
        )
        return meta

    @staticmethod
    def sanitize_filename(title: str, suffix: str = "_FA.srt") -> str:
        """Create a safe filename formatted as [Video_Title]_FA.srt."""
        cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]+', "", title).strip()
        cleaned = re.sub(r"\s+", "_", cleaned)
        if not cleaned:
            cleaned = "YouTube_Video"
        cleaned = cleaned[:65].strip("_")
        return f"[{cleaned}]{suffix}"

    @staticmethod
    def _consolidate_segments(raw_items: list[dict[str, Any]]) -> list[SubtitleSegment]:
        """Resolve timestamp overlaps and merge overly short fragments into readable subtitle lines."""
        cleaned_list: list[dict[str, Any]] = []
        for item in raw_items:
            text = str(item.get("text", "")).replace("\n", " ").strip()
            text = re.sub(r"\s+", " ", text)
            if not text:
                continue
            start = float(item.get("start", 0.0))
            duration = float(item.get("duration", 2.5))
            cleaned_list.append(
                {"text": text, "start": start, "end": start + max(0.8, duration)}
            )

        if not cleaned_list:
            return []

        for i in range(len(cleaned_list) - 1):
            next_start = cleaned_list[i + 1]["start"]
            if cleaned_list[i]["end"] > next_start:
                cleaned_list[i]["end"] = max(
                    cleaned_list[i]["start"] + 0.5, next_start - 0.05
                )

        merged: list[SubtitleSegment] = []
        idx = 0
        n = len(cleaned_list)

        while idx < n:
            curr = cleaned_list[idx]
            start_s = curr["start"]
            end_s = curr["end"]
            text_s = curr["text"]

            while (
                idx + 1 < n
                and (end_s - start_s) < 3.2
                and len(text_s) < 55
                and (cleaned_list[idx + 1]["start"] - end_s) < 0.45
                and not text_s.endswith((".", "?", "!"))
            ):
                idx += 1
                nxt = cleaned_list[idx]
                text_s = f"{text_s} {nxt['text']}"
                end_s = nxt["end"]

            merged.append(
                SubtitleSegment(
                    index=len(merged) + 1,
                    start_sec=round(start_s, 3),
                    end_sec=round(end_s, 3),
                    en_text=text_s,
                )
            )
            idx += 1

        return merged

    async def _fetch_raw_segments(
        self,
        video_id: str,
        progress_cb: ProgressCallback | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve raw transcript segments via TranscriptProvider and raise TranscriptFetchError on failure."""
        meta = self._meta_cache.get(video_id)
        dur_hint = meta.duration_seconds if meta else None

        if self.transcript_provider is None:
            raise TranscriptFetchError("error")

        res = await self.transcript_provider.get_segments(
            video_id=video_id,
            progress_cb=progress_cb,
            duration_hint_sec=dur_hint,
        )
        self._approximate_cache[video_id] = res.approximate
        self._approximate_cache.move_to_end(video_id)
        if len(self._approximate_cache) > self._max_cache_items:
            self._approximate_cache.popitem(last=False)

        if not res.segments:
            raise TranscriptFetchError(res.error_kind or "error")
        return res.segments

    async def _translate_batch(
        self,
        batch: list[SubtitleSegment],
        semaphore: asyncio.Semaphore,
    ) -> dict[int, str]:
        """Translate a batch of subtitle segments via LLMRouter (task='subtitle_batch', 90s budget)."""
        async with semaphore:
            payload = [{"index": seg.index, "text": seg.en_text} for seg in batch]
            prompt = (
                "Translate the following subtitle segments to fluent Persian:\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            )

            try:
                if self.llm_router is not None:
                    parsed_batch, _ = await self.llm_router.generate_json(
                        task="subtitle_batch",
                        system=TRANSLATION_SYSTEM_PROMPT,
                        prompt=prompt,
                        schema=TranslatedBatchSchema,
                        budget_sec=90.0,
                    )
                else:
                    response = await self.client.aio.models.generate_content(
                        model=self.primary_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=TRANSLATION_SYSTEM_PROMPT,
                            response_mime_type="application/json",
                            response_schema=TranslatedBatchSchema,
                            temperature=0.15,
                        ),
                    )
                    if isinstance(response.parsed, TranslatedBatchSchema):
                        parsed_batch = response.parsed
                    else:
                        parsed_batch = TranslatedBatchSchema.model_validate_json(
                            response.text or "{}"
                        )

                if parsed_batch and parsed_batch.items:
                    return {
                        item.index: item.fa_text.strip()
                        for item in parsed_batch.items
                    }

            except Exception as e:
                logger.error("Error translating subtitle batch: %s", e)

            return {seg.index: seg.en_text for seg in batch}

    async def get_or_create_translated_segments(
        self,
        video_id: str,
        progress_cb: ProgressCallback | None = None,
    ) -> list[SubtitleSegment]:
        """Fetch transcript via TranscriptProvider and translate chunks concurrently to Persian."""
        if video_id in self._segments_cache:
            self._segments_cache.move_to_end(video_id)
            if progress_cb:
                await progress_cb(100, t("sub_prog_cached"))
            return self._segments_cache[video_id]

        if progress_cb:
            await progress_cb(10, t("sub_prog_fetching"))

        raw_items = await self._fetch_raw_segments(
            video_id, progress_cb=progress_cb
        )
        segments = self._consolidate_segments(raw_items)
        if not segments:
            raise TranscriptFetchError("error")

        batches: list[list[SubtitleSegment]] = [
            segments[i : i + self.batch_size]
            for i in range(0, len(segments), self.batch_size)
        ]
        total_batches = len(batches)
        semaphore = asyncio.Semaphore(self.max_concurrent_batches)
        completed_batches = 0
        translations_map: dict[int, str] = {}

        async def _worker(batch_chunk: list[SubtitleSegment]) -> None:
            nonlocal completed_batches
            res = await self._translate_batch(batch_chunk, semaphore)
            translations_map.update(res)
            completed_batches += 1
            if progress_cb:
                pct = 25 + int((completed_batches / total_batches) * 70)
                await progress_cb(
                    pct,
                    t("sub_prog_translating_batch", current=completed_batches, total=total_batches),
                )

        if progress_cb:
            await progress_cb(
                25,
                t("sub_prog_starting_batches", lines=len(segments), batches=total_batches),
            )

        await asyncio.gather(*(_worker(b) for b in batches))

        for seg in segments:
            seg.fa_text = translations_map.get(seg.index, seg.en_text)

        self._segments_cache[video_id] = segments
        if len(self._segments_cache) > self._max_cache_items:
            self._segments_cache.popitem(last=False)

        if progress_cb:
            await progress_cb(100, t("sub_prog_translation_done"))

        return segments

    @staticmethod
    def build_srt_content(
        segments: list[SubtitleSegment], mode: str = "fa"
    ) -> str:
        """Build UTF-8 BOM SubRip (.srt) content with Unicode RLM (\u200f) on Persian lines."""
        blocks: list[str] = []
        rlm = "\u200f"

        for seg in segments:
            start_str = SubtitleSegment.format_srt_timestamp(seg.start_sec)
            end_str = SubtitleSegment.format_srt_timestamp(seg.end_sec)

            fa_line = f"{rlm}{seg.fa_text}" if seg.fa_text else seg.en_text
            en_line = seg.en_text

            if mode == "en":
                body = en_line
            elif mode == "bilingual":
                body = f"{fa_line}\n{en_line}"
            else:
                body = fa_line

            blocks.append(f"{seg.index}\n{start_str} --> {end_str}\n{body}")

        return "\ufeff" + "\n\n".join(blocks) + "\n"

    @staticmethod
    def get_preview_lines(segments: list[SubtitleSegment], count: int = 3) -> str:
        """Format the first `count` subtitle lines as a preview block for Telegram captions."""
        preview_items: list[str] = []
        for seg in segments[:count]:
            t_start = SubtitleSegment.format_short_time(seg.start_sec)
            t_end = SubtitleSegment.format_short_time(seg.end_sec)
            text = html.escape(seg.fa_text or seg.en_text)
            preview_items.append(f"<code>[{t_start} -> {t_end}]</code> {text}")
        return "\n".join(preview_items)

    async def generate_smart_summary(
        self,
        video_id: str,
        progress_cb: ProgressCallback | None = None,
    ) -> VideoSummarySchema:
        """Analyze the full video transcript and return a structured Persian summary with chapters."""
        if video_id in self._summary_cache:
            self._summary_cache.move_to_end(video_id)
            return self._summary_cache[video_id]

        if progress_cb:
            await progress_cb(15, t("sum_prog_fetching"))

        raw_items = await self._fetch_raw_segments(
            video_id, progress_cb=progress_cb
        )
        segments = self._consolidate_segments(raw_items)
        transcript_lines = [
            f"[{int(s.start_sec)}s] {s.en_text}" for s in segments
        ]
        full_transcript_text = "\n".join(transcript_lines)

        if progress_cb:
            await progress_cb(
                65, t("sum_prog_analyzing")
            )

        prompt = f"Analyze this timestamped YouTube transcript:\n\n{full_transcript_text[:90000]}"

        if self.llm_router is not None:
            summary, _ = await self.llm_router.generate_json(
                task="summary",
                system=SUMMARY_SYSTEM_PROMPT,
                prompt=prompt,
                schema=VideoSummarySchema,
                budget_sec=90.0,
            )
        else:
            response = await self.client.aio.models.generate_content(
                model=self.primary_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SUMMARY_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=VideoSummarySchema,
                    temperature=0.2,
                ),
            )
            if isinstance(response.parsed, VideoSummarySchema):
                summary = response.parsed
            else:
                summary = VideoSummarySchema.model_validate_json(
                    response.text or "{}"
                )

        self._summary_cache[video_id] = summary
        if len(self._summary_cache) > self._max_cache_items:
            self._summary_cache.popitem(last=False)

        if progress_cb:
            await progress_cb(100, t("sum_prog_done"))

        return summary

    async def create_softsub_video(
        self,
        video_id: str,
        work_dir: str | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> SoftsubMuxResult:
        """
        1. Fetch and translate Persian subtitles (or read from cache)
        2. Download MP4 stream via yt-dlp using build_opts('download')
        3. Mux Persian (fas) and English (eng) softsub tracks via FFmpeg (-c copy)
        """
        async def _sub_progress(pct: int, msg: str) -> None:
            if progress_cb:
                scaled_pct = int(pct * 0.50)
                await progress_cb(scaled_pct, msg)

        segments = await self.get_or_create_translated_segments(
            video_id, progress_cb=_sub_progress
        )
        approx = self.is_approximate(video_id)

        if work_dir is None:
            import tempfile

            work_dir = tempfile.mkdtemp(prefix=f"vsf_mux_{video_id}_")

        srt_fa_path = os.path.join(work_dir, "sub_fa.srt")
        srt_en_path = os.path.join(work_dir, "sub_en.srt")

        with open(srt_fa_path, "w", encoding="utf-8") as f_fa:
            f_fa.write(self.build_srt_content(segments, mode="fa"))

        with open(srt_en_path, "w", encoding="utf-8") as f_en:
            f_en.write(self.build_srt_content(segments, mode="en"))

        if progress_cb:
            await progress_cb(
                60,
                t("mux_prog_downloading_video"),
            )

        raw_video_path = os.path.join(work_dir, "raw_video.mp4")

        def _download_video_sync() -> dict[str, Any]:
            ydl_opts = build_opts(
                "download",
                format=(
                    "best[ext=mp4][filesize<47M]/"
                    "bestvideo[ext=mp4][height<=720][filesize<40M]+bestaudio[ext=m4a]/"
                    "best[ext=mp4][height<=480]/"
                    "best[ext=mp4]"
                ),
                outtmpl=raw_video_path,
                merge_output_format="mp4",
            )
            url = f"https://www.youtube.com/watch?v={video_id}"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info or {}

        info = await asyncio.to_thread(_download_video_sync)

        if not os.path.exists(raw_video_path):
            candidates_files = [
                os.path.join(work_dir, f)
                for f in os.listdir(work_dir)
                if f.startswith("raw_video")
            ]
            if not candidates_files:
                raise RuntimeError(t("mux_err_download_failed"))
            raw_video_path = candidates_files[0]

        thumb_path = os.path.join(work_dir, "thumb.jpg")
        thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        try:
            await asyncio.to_thread(urllib.request.urlretrieve, thumb_url, thumb_path)
        except Exception:
            thumb_path = None

        if progress_cb:
            await progress_cb(
                88,
                t("mux_prog_ffmpeg"),
            )

        muxed_output_path = os.path.join(work_dir, "output_softsub_fa.mp4")
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            raw_video_path,
            "-i",
            srt_fa_path,
            "-i",
            srt_en_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map",
            "1:0",
            "-map",
            "2:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=fas",
            "-metadata:s:s:0",
            "handler_name=Persian",
            "-metadata:s:s:0",
            "title=فارسی (AI Softsub)",
            "-disposition:s:0",
            "default",
            "-metadata:s:s:1",
            "language=eng",
            "-metadata:s:s:1",
            "handler_name=English",
            "-metadata:s:s:1",
            "title=English (Original)",
            "-movflags",
            "+faststart",
            muxed_output_path,
        ]

        returncode, _, stderr = await JobManager.run_subprocess_safe(
            ffmpeg_cmd, timeout=45.0
        )

        if returncode != 0 or not os.path.exists(muxed_output_path):
            err_msg = stderr.decode("utf-8", errors="ignore")[:300]
            logger.error("FFmpeg softsub muxing failed: %s", err_msg)
            raise RuntimeError(t("mux_err_ffmpeg"))

        if progress_cb:
            await progress_cb(100, t("mux_prog_ready_uploading"))

        title = str(info.get("title") or f"YouTube_{video_id}")
        channel = str(info.get("channel") or info.get("uploader") or "YouTube")
        duration_sec = int(info.get("duration") or 0)
        width = int(info.get("width") or 1280)
        height = int(info.get("height") or 720)
        file_size = os.path.getsize(muxed_output_path)

        return SoftsubMuxResult(
            muxed_video_path=muxed_output_path,
            srt_fa_path=srt_fa_path,
            thumbnail_path=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            title=title,
            channel=channel,
            duration_seconds=duration_sec,
            width=width,
            height=height,
            file_size_bytes=file_size,
            work_dir_path=work_dir,
            approximate_timing=approx,
        )

    @staticmethod
    def build_interactive_web_player_html(
        meta: VideoMetadataInfo,
        segments: list[SubtitleSegment],
        start_seconds: int = 0,
    ) -> str:
        """Build a standalone HTML5 bilingual interactive web player."""
        subs_json = json.dumps(
            [
                {
                    "s": seg.start_sec,
                    "e": seg.end_sec,
                    "fa": seg.fa_text or seg.en_text,
                    "en": seg.en_text,
                    "t": SubtitleSegment.format_short_time(seg.start_sec),
                }
                for seg in segments
            ],
            ensure_ascii=False,
        )
        # Subtitle text comes from YouTube captions (uploader-controlled): make the JSON safe
        # to embed inside <script> so "</script>" or "<!--" cannot break out of it.
        subs_json = (
            subs_json.replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        safe_video_id = json.dumps(str(meta.video_id))
        safe_title = html.escape(meta.title)
        safe_channel = html.escape(meta.channel)

        return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title}</title>
<style>
  :root {{ --bg: #0f172a; --card: #1e293b; --accent: #38bdf8; --text: #f8fafc; --muted: #94a3b8; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, 'Segoe UI', Tahoma, sans-serif; }}
  body {{ background: var(--bg); color: var(--text); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }}
  header {{ padding: 12px 20px; background: #020617; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; }}
  header h1 {{ font-size: 1rem; color: var(--accent); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .main-container {{ display: flex; flex: 1; overflow: hidden; flex-direction: row; }}
  .player-col {{ flex: 2; display: flex; flex-direction: column; background: #000; position: relative; }}
  #yt-player {{ width: 100%; flex: 1; border: none; }}
  .live-sub-box {{ background: rgba(2, 6, 23, 0.95); padding: 14px 20px; text-align: center; min-height: 82px; border-top: 2px solid var(--accent); display: flex; flex-direction: column; justify-content: center; gap: 6px; }}
  .sub-fa {{ font-size: 1.2rem; font-weight: 700; color: #fde047; line-height: 1.5; }}
  .sub-en {{ font-size: 0.9rem; color: var(--muted); direction: ltr; }}
  .transcript-col {{ flex: 1; background: var(--card); border-right: 1px solid #334155; overflow-y: auto; padding: 12px; }}
  .seg-item {{ padding: 10px 12px; border-radius: 8px; margin-bottom: 8px; cursor: pointer; transition: 0.2s; border: 1px solid transparent; }}
  .seg-item:hover {{ background: #334155; }}
  .seg-item.active {{ background: #0369a1; border-color: var(--accent); }}
  .seg-time {{ font-size: 0.78rem; color: var(--accent); font-family: monospace; margin-bottom: 4px; display: inline-block; }}
  .seg-fa {{ font-size: 0.95rem; margin-bottom: 4px; line-height: 1.4; }}
  .seg-en {{ font-size: 0.8rem; color: var(--muted); direction: ltr; text-align: left; }}
  @media (max-width: 768px) {{ .main-container {{ flex-direction: column; }} .player-col {{ flex: none; height: 48vh; }} }}
</style>
</head>
<body>
<header><h1>🎬 {safe_title} ({safe_channel})</h1></header>
<div class="main-container">
  <div class="player-col">
    <div id="yt-player"></div>
    <div class="live-sub-box">
      <div id="live-fa" class="sub-fa">▶️ ویدیو را پخش کنید تا زیرنویس فارسی نمایش داده شود...</div>
      <div id="live-en" class="sub-en"></div>
    </div>
  </div>
  <div class="transcript-col" id="transcript-list"></div>
</div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
  const SUBS = {subs_json};
  let player = null;
  let activeIdx = -1;
  const listEl = document.getElementById('transcript-list');
  SUBS.forEach((item, idx) => {{
    const div = document.createElement('div');
    div.className = 'seg-item';
    div.id = 'seg-' + idx;
    const timeEl = document.createElement('span');
    timeEl.className = 'seg-time';
    timeEl.textContent = '[' + item.t + ']';
    const faEl = document.createElement('div');
    faEl.className = 'seg-fa';
    faEl.textContent = item.fa;
    const enEl = document.createElement('div');
    enEl.className = 'seg-en';
    enEl.textContent = item.en;
    div.append(timeEl, faEl, enEl);
    div.onclick = () => {{ if (player && player.seekTo) {{ player.seekTo(item.s, true); player.playVideo(); }} }};
    listEl.appendChild(div);
  }});
  function onYouTubeIframeAPIReady() {{
    player = new YT.Player('yt-player', {{
      videoId: {safe_video_id},
      playerVars: {{ autoplay: 1, start: {max(0, int(start_seconds))}, rel: 0, cc_load_policy: 0 }},
      events: {{ onReady: () => setInterval(syncSubtitles, 150) }}
    }});
  }}
  function syncSubtitles() {{
    if (!player || typeof player.getCurrentTime !== 'function') return;
    const t = player.getCurrentTime();
    const idx = SUBS.findIndex(s => t >= s.s && t <= s.e + 0.3);
    if (idx !== -1 && idx !== activeIdx) {{
      activeIdx = idx;
      document.getElementById('live-fa').textContent = SUBS[idx].fa;
      document.getElementById('live-en').textContent = SUBS[idx].en;
      document.querySelectorAll('.seg-item.active').forEach(el => el.classList.remove('active'));
      const activeEl = document.getElementById('seg-' + idx);
      if (activeEl) {{
        activeEl.classList.add('active');
        activeEl.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
      }}
    }}
  }}
</script>
</body>
</html>"""
