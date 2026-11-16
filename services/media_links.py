import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import yt_dlp

from config import Settings
from services.i18n import t
from services.job_manager import JobManager, JobRejectedError
from services.link_server import LinkServer
from services.subtitle_service import SubtitleService
from services.ytdlp_options import build_opts

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str], Awaitable[None]]
LinkKind = Literal["link_sub", "link_orig", "link_audio"]


@dataclass
class CreatedMediaLink:
    """Result returned after generating a direct streaming link."""

    url: str
    filename: str
    expires_at_unix: int
    ttl_sec: int
    size_bytes: int
    title: str
    channel: str
    kind: LinkKind
    approximate_timing: bool = False


class MediaLinkService:
    """
    Generates direct streaming links (softsub MP4, original progressive MP4 <=720p,
    or M4A audio) stored in LinkServer without uploading large media to Telegram.
    """

    VIDEO_FORMAT_720P = (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
        "best[ext=mp4][height<=720]/"
        "best[ext=mp4]"
    )
    AUDIO_FORMAT_M4A = "bestaudio[ext=m4a]/bestaudio"

    def __init__(
        self,
        settings: Settings,
        subtitle_service: SubtitleService,
        link_server: LinkServer,
        job_manager: JobManager,
    ) -> None:
        self.settings = settings
        self.subtitle_service = subtitle_service
        self.link_server = link_server
        self.job_manager = job_manager

    def check_link_duration(
        self, duration_seconds: int | None, user_id: int | None = None
    ) -> None:
        """Enforce LINK_MAX_DURATION_SEC before queuing a link job."""
        if (
            duration_seconds is not None
            and duration_seconds > self.settings.LINK_MAX_DURATION_SEC
        ):
            self.job_manager._spawn_analytics(
                user_id, "job_rejected", reason="link_video_too_long"
            )
            max_mins = self.settings.LINK_MAX_DURATION_SEC // 60
            raise JobRejectedError(
                "link_video_too_long",
                t("job_err_link_too_long", max_mins=max_mins),
            )

    @staticmethod
    def _clean_stream_filename(title: str, suffix: str) -> str:
        cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]+', "", title).strip()
        cleaned = re.sub(r"\s+", "_", cleaned)
        cleaned = re.sub(r"[^A-Za-z0-9._\-]", "", cleaned)
        if not cleaned:
            cleaned = "YouTube_Media"
        return f"{cleaned[:55].strip('_')}{suffix}"

    def _download_media_sync(
        self, video_id: str, out_template: str, is_audio: bool
    ) -> dict[str, Any]:
        fmt = self.AUDIO_FORMAT_M4A if is_audio else self.VIDEO_FORMAT_720P
        extra_opts: dict[str, Any] = {
            "format": fmt,
            "outtmpl": out_template,
        }
        if not is_audio:
            extra_opts["merge_output_format"] = "mp4"

        ydl_opts = build_opts(
            "download",
            max_filesize_mb=self.settings.LINK_MAX_FILESIZE_MB,
            **extra_opts,
        )
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info or {}

    @staticmethod
    def _locate_downloaded_file(work_dir: str, prefix: str) -> str:
        candidates = [
            os.path.join(work_dir, f)
            for f in os.listdir(work_dir)
            if f.startswith(prefix) and not f.endswith(".part")
        ]
        if not candidates:
            raise RuntimeError(t("media_err_download_failed"))
        return candidates[0]

    async def create_link(
        self,
        video_id: str,
        kind: LinkKind,
        progress_cb: ProgressCallback | None = None,
    ) -> CreatedMediaLink:
        if not self.link_server.is_enabled:
            raise RuntimeError(t("link_err_not_enabled"))

        meta = await self.subtitle_service.get_video_metadata(video_id)
        approx_timing = False

        with self.job_manager.create_job_dir(f"{kind}_{video_id}_") as work_dir:
            if kind == "link_sub":
                async def _sub_prog(pct: int, msg: str) -> None:
                    if progress_cb:
                        await progress_cb(int(pct * 0.45), msg)

                segments = await asyncio.wait_for(
                    self.subtitle_service.get_or_create_translated_segments(
                        video_id, progress_cb=_sub_prog
                    ),
                    timeout=float(self.settings.LINK_TRANSLATION_TIMEOUT_SEC),
                )
                approx_timing = self.subtitle_service.is_approximate(video_id)

                srt_fa_path = os.path.join(work_dir, "sub_fa.srt")
                srt_en_path = os.path.join(work_dir, "sub_en.srt")
                with open(srt_fa_path, "w", encoding="utf-8") as f_fa:
                    f_fa.write(
                        self.subtitle_service.build_srt_content(segments, mode="fa")
                    )
                with open(srt_en_path, "w", encoding="utf-8") as f_en:
                    f_en.write(
                        self.subtitle_service.build_srt_content(segments, mode="en")
                    )

                if progress_cb:
                    await progress_cb(
                        55, t("link_prog_sub_done_downloading")
                    )

                raw_template = os.path.join(work_dir, "raw_media.%(ext)s")
                info = await asyncio.to_thread(
                    self._download_media_sync, video_id, raw_template, False
                )
                raw_video_path = self._locate_downloaded_file(work_dir, "raw_media")

                if progress_cb:
                    await progress_cb(88, t("link_prog_muxing"))

                muxed_path = os.path.join(work_dir, "muxed_softsub.mp4")
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
                    "title=Persian (FA)",
                    "-disposition:s:0",
                    "default",
                    "-metadata:s:s:1",
                    "language=eng",
                    "-metadata:s:s:1",
                    "handler_name=English",
                    "-metadata:s:s:1",
                    "title=English (EN)",
                    "-movflags",
                    "+faststart",
                    muxed_path,
                ]
                rc, _, stderr = await JobManager.run_subprocess_safe(
                    ffmpeg_cmd, timeout=60.0
                )
                if rc != 0 or not os.path.exists(muxed_path):
                    err_str = stderr.decode("utf-8", errors="ignore")[:250]
                    logger.error("FFmpeg link_sub mux failed: %s", err_str)
                    raise RuntimeError(t("link_err_mux_failed"))

                final_temp_path = muxed_path
                dl_filename = self._clean_stream_filename(
                    str(info.get("title") or meta.title), "_SoftSub_FA.mp4"
                )
                content_type = "video/mp4"

            elif kind == "link_orig":
                if progress_cb:
                    await progress_cb(25, t("link_prog_downloading_orig"))
                raw_template = os.path.join(work_dir, "orig_media.%(ext)s")
                info = await asyncio.to_thread(
                    self._download_media_sync, video_id, raw_template, False
                )
                final_temp_path = self._locate_downloaded_file(work_dir, "orig_media")
                dl_filename = self._clean_stream_filename(
                    str(info.get("title") or meta.title), "_720p.mp4"
                )
                content_type = "video/mp4"

            else:  # link_audio
                if progress_cb:
                    await progress_cb(25, t("link_prog_downloading_audio"))
                raw_template = os.path.join(work_dir, "audio_media.%(ext)s")
                info = await asyncio.to_thread(
                    self._download_media_sync, video_id, raw_template, True
                )
                downloaded_audio = self._locate_downloaded_file(work_dir, "audio_media")
                if not downloaded_audio.lower().endswith(".m4a"):
                    m4a_path = os.path.join(work_dir, "converted_audio.m4a")
                    rc, _, _ = await JobManager.run_subprocess_safe(
                        [
                            "ffmpeg",
                            "-y",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-i",
                            downloaded_audio,
                            "-vn",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "128k",
                            "-movflags",
                            "+faststart",
                            m4a_path,
                        ],
                        timeout=60.0,
                    )
                    if rc == 0 and os.path.exists(m4a_path):
                        final_temp_path = m4a_path
                    else:
                        final_temp_path = downloaded_audio
                else:
                    final_temp_path = downloaded_audio

                dl_filename = self._clean_stream_filename(
                    str(info.get("title") or meta.title), "_Audio.m4a"
                )
                content_type = "audio/mp4"

            if progress_cb:
                await progress_cb(95, t("link_prog_registering"))

            url, exp_ts, size_bytes = await self.link_server.register_file(
                source_file_path=final_temp_path,
                download_filename=dl_filename,
                content_type=content_type,
                ttl_sec=self.settings.LINK_TTL_SEC,
            )

            title = str(info.get("title") or meta.title)
            channel = str(info.get("channel") or info.get("uploader") or meta.channel)

            if progress_cb:
                await progress_cb(100, t("link_prog_done"))

            return CreatedMediaLink(
                url=url,
                filename=dl_filename,
                expires_at_unix=exp_ts,
                ttl_sec=self.settings.LINK_TTL_SEC,
                size_bytes=size_bytes,
                title=title,
                channel=channel,
                kind=kind,
                approximate_timing=approx_timing,
            )
