import importlib.util
import logging
import shutil
from typing import Any, Literal

from config import settings

logger = logging.getLogger(__name__)

_startup_checked = False


def build_opts(
    kind: Literal["metadata", "download"],
    max_filesize_mb: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """
    Build standardized yt-dlp options for metadata probes and video downloads.
    Allows overriding `max_filesize_mb` (e.g. for direct streaming link jobs).
    Note: YouTubeSearcher (extract_flat searches) intentionally does NOT use this proxy.
    """
    opts: dict[str, Any] = {
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 2,
        "quiet": True,
        "no_warnings": True,
    }

    proxy_url = settings.YT_PROXY_HTTP or settings.YT_PROXY_HTTPS
    if proxy_url:
        opts["proxy"] = proxy_url

    if kind == "metadata":
        opts["skip_download"] = True
    elif kind == "download":
        cap_mb = (
            int(max_filesize_mb)
            if max_filesize_mb is not None
            else int(settings.SOFTSUB_MAX_FILESIZE_MB)
        )
        opts["max_filesize"] = cap_mb * 1024 * 1024

    opts.update(extra)
    return opts


def check_ytdlp_runtime_dependencies(log_result: bool = True) -> tuple[bool, bool]:
    """
    Check whether `deno` is on PATH and `yt_dlp_ejs` is importable.
    Logs the status once at startup when `log_result=True`.
    """
    global _startup_checked
    deno_path = shutil.which("deno")
    deno_found = deno_path is not None
    ejs_found = importlib.util.find_spec("yt_dlp_ejs") is not None

    if log_result and not _startup_checked:
        _startup_checked = True
        if deno_found and ejs_found:
            logger.info(
                "yt-dlp JS runtime check ok: deno=%s, yt_dlp_ejs=installed",
                deno_path,
            )
        else:
            logger.warning(
                "yt-dlp JS runtime check incomplete: deno_found=%s, yt_dlp_ejs_found=%s (YouTube downloads may fail without both)",
                deno_found,
                ejs_found,
            )

    return deno_found, ejs_found
