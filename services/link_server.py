import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from aiohttp import web

from config import Settings

logger = logging.getLogger(__name__)


class _LimitedFileResponse(web.FileResponse):
    """FileResponse that holds a semaphore for the whole time the file is being sent."""

    def __init__(self, semaphore: asyncio.Semaphore, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._limit_sem = semaphore

    async def prepare(self, request: web.BaseRequest) -> Any:
        async with self._limit_sem:
            return await super().prepare(request)


@dataclass
class StoredLinkEntry:
    """Metadata for a single served media file inside LINK_DIR."""

    file_id: str
    disk_filename: str
    download_filename: str
    content_type: str
    size_bytes: int
    created_at: float
    expires_at: float


class LinkServer:
    """
    Lightweight aiohttp HTTP file server supporting HTTP Range (206 Partial Content),
    HMAC-SHA256 signed expiring tokens, per-IP rate limiting, concurrent download caps,
    persistent JSON index rebuild on startup, and background TTL/quota sweeping.
    """

    SWEEP_INTERVAL_SEC = 120.0
    INDEX_FILENAME = ".links_index.json"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url: str | None = settings.LINK_PUBLIC_BASE_URL
        self.link_dir: str = os.path.abspath(settings.LINK_DIR)

        if settings.LINK_SIGNING_SECRET:
            raw_secret = settings.LINK_SIGNING_SECRET.get_secret_value().encode("utf-8")
        else:
            salt_bytes = settings.ANALYTICS_SALT.get_secret_value().encode("utf-8")
            raw_secret = hmac.new(
                salt_bytes, b"vsf_link_signing_key_v1", hashlib.sha256
            ).digest()
        self._signing_key: bytes = raw_secret

        self._entries: dict[str, StoredLinkEntry] = {}
        self._ip_requests: dict[str, deque[float]] = {}
        self._download_sem = asyncio.Semaphore(
            max(1, int(settings.LINK_MAX_CONCURRENT_DOWNLOADS))
        )
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_enabled(self) -> bool:
        return bool(self.base_url)

    def _index_path(self) -> str:
        return os.path.join(self.link_dir, self.INDEX_FILENAME)

    def _load_or_rebuild_index_sync(self) -> None:
        os.makedirs(self.link_dir, exist_ok=True)
        idx_path = self._index_path()
        now = time.time()
        loaded: dict[str, StoredLinkEntry] = {}

        if os.path.exists(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for fid, item in raw.items():
                        if not isinstance(item, dict):
                            continue
                        disk_fn = str(item.get("disk_filename", ""))
                        full_p = os.path.join(self.link_dir, disk_fn)
                        if not disk_fn or not os.path.isfile(full_p):
                            continue
                        loaded[fid] = StoredLinkEntry(
                            file_id=str(fid),
                            disk_filename=disk_fn,
                            download_filename=str(
                                item.get("download_filename") or disk_fn
                            ),
                            content_type=str(
                                item.get("content_type") or "video/mp4"
                            ),
                            size_bytes=int(os.path.getsize(full_p)),
                            created_at=float(item.get("created_at") or now),
                            expires_at=float(
                                item.get("expires_at")
                                or (now + self.settings.LINK_TTL_SEC)
                            ),
                        )
            except Exception as exc:
                logger.warning("Failed to read link index file, rebuilding: %s", exc)

        # Scan LINK_DIR for any media files not in index
        known_disk_files = {e.disk_filename for e in loaded.values()}
        for entry in os.scandir(self.link_dir):
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.name == self.INDEX_FILENAME or entry.name.startswith("."):
                continue
            if entry.name in known_disk_files:
                continue
            m = re.match(r"^([a-f0-9]{12,32})_(.+)$", entry.name)
            fid = m.group(1) if m else hashlib.sha256(entry.name.encode()).hexdigest()[:16]
            dl_name = m.group(2) if m else entry.name
            stat = entry.stat(follow_symlinks=False)
            ctype = "audio/mp4" if entry.name.lower().endswith(".m4a") else "video/mp4"
            loaded[fid] = StoredLinkEntry(
                file_id=fid,
                disk_filename=entry.name,
                download_filename=dl_name,
                content_type=ctype,
                size_bytes=int(stat.st_size),
                created_at=float(stat.st_mtime),
                expires_at=float(stat.st_mtime + self.settings.LINK_TTL_SEC),
            )

        self._entries = loaded
        self._save_index_sync()

    def _save_index_sync(self) -> None:
        os.makedirs(self.link_dir, exist_ok=True)
        idx_path = self._index_path()
        tmp_path = f"{idx_path}.tmp"
        try:
            payload = {fid: asdict(entry) for fid, entry in self._entries.items()}
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, idx_path)
        except Exception as exc:
            logger.warning("Failed to persist link index: %s", exc)

    def _sign_token(self, file_id: str, expires_at: int) -> str:
        msg = f"{file_id}:{expires_at}".encode("ascii")
        sig = hmac.new(self._signing_key, msg, hashlib.sha256).digest()[:16]
        sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        return f"{file_id}.{expires_at}.{sig_b64}"

    def _verify_token(self, token: str) -> tuple[str | None, str]:
        """Return (file_id, status) where status is 'ok', 'expired', or 'invalid'."""
        parts = token.split(".")
        if len(parts) != 3:
            return None, "invalid"
        file_id, exp_str, sig_b64 = parts
        if not re.fullmatch(r"[a-f0-9]{12,32}", file_id) or not exp_str.isdigit():
            return None, "invalid"
        exp_ts = int(exp_str)
        expected_token = self._sign_token(file_id, exp_ts)
        if not hmac.compare_digest(token, expected_token):
            return None, "invalid"
        if time.time() > exp_ts:
            return None, "expired"
        return file_id, "ok"

    def _check_rate_limit(self, client_ip: str) -> bool:
        now = time.monotonic()
        dq = self._ip_requests.setdefault(client_ip, deque())
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= self.settings.LINK_RATE_LIMIT_PER_MIN:
            return False
        dq.append(now)
        if len(self._ip_requests) > 2000:
            oldest_ip = next(iter(self._ip_requests))
            self._ip_requests.pop(oldest_ip, None)
        return True

    async def _handle_file_request(self, request: web.Request) -> web.StreamResponse:
        # The link server is exposed directly, so X-Forwarded-For is attacker-controlled
        # and must not be used for rate limiting. Use the real peer address.
        client_ip = request.remote or "unknown"
        if not self._check_rate_limit(client_ip):
            raise web.HTTPTooManyRequests(text="Rate limit exceeded. Try again in a minute.")

        token = request.match_info.get("token", "")
        file_id, status = self._verify_token(token)
        if status == "invalid":
            raise web.HTTPForbidden(text="Invalid link token.")
        if status == "expired":
            raise web.HTTPForbidden(text="Link has expired.")

        entry = self._entries.get(file_id or "")
        if entry is None or time.time() > entry.expires_at:
            raise web.HTTPNotFound(text="File not found or expired.")

        full_path = os.path.join(self.link_dir, entry.disk_filename)
        if not os.path.isfile(full_path):
            raise web.HTTPNotFound(text="File not found.")

        safe_disp_name = re.sub(r'["\\\r\n]', "_", entry.download_filename)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": entry.content_type,
            "Content-Disposition": f'inline; filename="{safe_disp_name}"',
            "X-Content-Type-Options": "nosniff",
        }

        # aiohttp streams the file in prepare(), after the handler returns, so the
        # concurrency cap must be held there rather than around the return statement.
        return _LimitedFileResponse(self._download_sem, path=full_path, headers=headers)

    async def _handle_not_found(self, _request: web.Request) -> web.StreamResponse:
        raise web.HTTPNotFound(text="Not Found")

    async def register_file(
        self,
        source_file_path: str,
        download_filename: str,
        content_type: str,
        ttl_sec: int | None = None,
    ) -> tuple[str, int, int]:
        """
        Move `source_file_path` into LINK_DIR, enforce total disk cap, sign an expiring URL,
        and return (public_url, expires_at_unix, size_bytes).
        """
        if not self.base_url:
            raise RuntimeError("Direct link server is not enabled.")

        effective_ttl = int(ttl_sec or self.settings.LINK_TTL_SEC)
        now = time.time()
        exp_ts = int(now + effective_ttl)
        file_id = hashlib.sha256(
            f"{download_filename}:{now}:{os.urandom(8).hex()}".encode("utf-8")
        ).hexdigest()[:16]

        safe_dl_name = re.sub(r"[^A-Za-z0-9._\-()\[\]]+", "_", download_filename).strip("_")
        if not safe_dl_name:
            safe_dl_name = "media.mp4"
        disk_filename = f"{file_id}_{safe_dl_name}"
        dest_path = os.path.join(self.link_dir, disk_filename)

        async with self._lock:
            def _move_sync() -> int:
                os.makedirs(self.link_dir, exist_ok=True)
                import shutil

                shutil.move(source_file_path, dest_path)
                return int(os.path.getsize(dest_path))

            size_bytes = await asyncio.to_thread(_move_sync)
            self._entries[file_id] = StoredLinkEntry(
                file_id=file_id,
                disk_filename=disk_filename,
                download_filename=safe_dl_name,
                content_type=content_type,
                size_bytes=size_bytes,
                created_at=now,
                expires_at=float(exp_ts),
            )
            await asyncio.to_thread(self._sweep_sync)

        token = self._sign_token(file_id, exp_ts)
        url = f"{self.base_url}/f/{token}/{quote(safe_dl_name)}"
        return url, exp_ts, size_bytes

    def _sweep_sync(self) -> tuple[int, int]:
        """Remove expired files and evict oldest files if total size exceeds LINK_MAX_TOTAL_MB."""
        os.makedirs(self.link_dir, exist_ok=True)
        now = time.time()
        expired_removed = 0
        evicted_removed = 0

        for fid, entry in list(self._entries.items()):
            fpath = os.path.join(self.link_dir, entry.disk_filename)
            if now > entry.expires_at or not os.path.isfile(fpath):
                with contextlib.suppress(OSError):
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                self._entries.pop(fid, None)
                expired_removed += 1

        max_bytes = int(self.settings.LINK_MAX_TOTAL_MB) * 1024 * 1024
        total_bytes = sum(e.size_bytes for e in self._entries.values())

        if total_bytes > max_bytes:
            sorted_oldest = sorted(
                self._entries.values(), key=lambda x: x.created_at
            )
            for entry in sorted_oldest:
                if total_bytes <= max_bytes:
                    break
                fpath = os.path.join(self.link_dir, entry.disk_filename)
                with contextlib.suppress(OSError):
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                total_bytes -= entry.size_bytes
                self._entries.pop(entry.file_id, None)
                evicted_removed += 1

        self._save_index_sync()
        return expired_removed, evicted_removed

    async def _sweeper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.SWEEP_INTERVAL_SEC)
                async with self._lock:
                    exp_cnt, ev_cnt = await asyncio.to_thread(self._sweep_sync)
                if exp_cnt or ev_cnt:
                    logger.info(
                        "LinkServer sweeper: expired_removed=%d evicted_removed=%d active=%d",
                        exp_cnt,
                        ev_cnt,
                        len(self._entries),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("LinkServer sweeper error: %s", exc)

    def get_stats(self) -> dict[str, Any]:
        total_bytes = sum(e.size_bytes for e in self._entries.values())
        return {
            "enabled": self.is_enabled,
            "active_links": len(self._entries),
            "total_mb": round(total_bytes / (1024 * 1024), 2),
        }

    async def start(self) -> None:
        if not self.is_enabled:
            logger.info("LINK_PUBLIC_BASE_URL is empty; LinkServer is disabled.")
            return

        await asyncio.to_thread(self._load_or_rebuild_index_sync)
        async with self._lock:
            await asyncio.to_thread(self._sweep_sync)

        app = web.Application()
        app.router.add_get("/f/{token}/{filename}", self._handle_file_request)
        app.router.add_route("*", "/{tail:.*}", self._handle_not_found)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self.settings.LINK_BIND_HOST,
            port=self.settings.LINK_BIND_PORT,
        )
        await self._site.start()
        self._sweeper_task = asyncio.create_task(self._sweeper_loop())
        logger.info(
            "LinkServer listening on %s:%d (dir=%s, active_files=%d)",
            self.settings.LINK_BIND_HOST,
            self.settings.LINK_BIND_PORT,
            self.link_dir,
            len(self._entries),
        )

    async def stop(self) -> None:
        if self._sweeper_task and not self._sweeper_task.done():
            self._sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper_task
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
            self._site = None
