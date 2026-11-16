import asyncio
import contextlib
import logging
import os
import shutil
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aiogram import Bot

from config import Settings
from middlewares.analytics_middleware import current_user_id_var
from services.analytics import Analytics
from services.ytdlp_options import check_ytdlp_runtime_dependencies

logger = logging.getLogger(__name__)

QueuePositionCallback = Callable[[int], Awaitable[None]]
ShutdownNotifyCallback = Callable[[str], Awaitable[None]]


class JobRejectedError(RuntimeError):
    """Raised when a job is refused due to queue capacity, user rate limits, duration, disk space, or shutdown."""

    def __init__(self, reason: str, user_message_fa: str) -> None:
        self.reason = reason
        self.user_message_fa = user_message_fa
        super().__init__(user_message_fa)


@dataclass
class _FIFOQueuePool:
    max_concurrent: int
    max_queue_len: int
    active_count: int = 0
    waiters: deque[asyncio.Event] = field(default_factory=deque)


@dataclass
class _ActiveHeavyJob:
    job_id: str
    user_id: int | None
    kind: str
    task: asyncio.Task[Any] | None
    notify_cb: ShutdownNotifyCallback | None
    work_dirs: set[str] = field(default_factory=set)


class JobManager:
    """
    Resource safety, concurrency queuing, per-user rate limiting, work-directory
    lifecycle management, subprocess cleanup, health watchdog, and graceful shutdown.
    """

    SWEEP_INTERVAL_SEC = 600.0  # 10 minutes
    STALE_DIR_MAX_AGE_SEC = 1800.0  # 30 minutes
    WATCHDOG_INTERVAL_SEC = 300.0  # 5 minutes
    ALERT_COOLDOWN_SEC = 3600.0  # 1 hour per problem type

    def __init__(
        self,
        settings: Settings,
        analytics: Analytics | None = None,
    ) -> None:
        self.settings = settings
        self.analytics = analytics
        self.is_shutting_down = False

        os.makedirs(self.settings.WORK_DIR, exist_ok=True)

        self._pools: dict[str, _FIFOQueuePool] = {
            "video": _FIFOQueuePool(
                max_concurrent=settings.MAX_CONCURRENT_VIDEO_JOBS,
                max_queue_len=settings.QUEUE_MAX_LENGTH,
            ),
            "gemini_url": _FIFOQueuePool(
                max_concurrent=settings.MAX_CONCURRENT_GEMINI_URL_JOBS,
                max_queue_len=settings.QUEUE_MAX_LENGTH,
            ),
            "translation": _FIFOQueuePool(
                max_concurrent=settings.MAX_CONCURRENT_TRANSLATION_JOBS,
                max_queue_len=settings.QUEUE_MAX_LENGTH,
            ),
        }

        self._active_users: set[int] = set()
        self._user_hourly_history: dict[int, deque[float]] = {}
        self._active_jobs: dict[str, _ActiveHeavyJob] = {}

        self._bot: Bot | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_alerts: dict[str, float] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_analytics(
        self, user_id: int | None, event_type: str, **props: Any
    ) -> None:
        if not self.analytics:
            return
        uid = user_id if user_id is not None else current_user_id_var.get()
        try:
            task = asyncio.create_task(
                self.analytics.log_event(uid, event_type, **props)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # Background Tasks: Workdir Sweep & 5-Minute Health Watchdog
    # ─────────────────────────────────────────────────────────────

    async def start_background_tasks(self, bot: Bot) -> None:
        """Run initial startup workdir sweep and launch periodic sweep and watchdog tasks."""
        self._bot = bot
        check_ytdlp_runtime_dependencies(log_result=True)
        await self.sweep_old_work_dirs()

        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _sweep_loop(self) -> None:
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self.SWEEP_INTERVAL_SEC)
                await self.sweep_old_work_dirs()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Workdir sweep error: %s", exc)

    async def sweep_old_work_dirs(self) -> int:
        """Delete subdirectories inside WORK_DIR older than 30 minutes."""
        def _sweep_sync() -> int:
            base = self.settings.WORK_DIR
            if not os.path.exists(base):
                os.makedirs(base, exist_ok=True)
                return 0
            now = time.time()
            removed = 0
            active_paths = {
                d for job in self._active_jobs.values() for d in job.work_dirs
            }
            for entry in os.scandir(base):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                full_path = os.path.abspath(entry.path)
                if full_path in active_paths:
                    continue
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                    if now - mtime >= self.STALE_DIR_MAX_AGE_SEC:
                        shutil.rmtree(full_path, ignore_errors=True)
                        removed += 1
                except Exception:
                    pass
            return removed

        removed_count = await asyncio.to_thread(_sweep_sync)
        logger.info(
            "Workdir cleanup sweep finished: removed %d stale subdirectories from %s",
            removed_count,
            self.settings.WORK_DIR,
        )
        if removed_count > 0:
            self._spawn_analytics(None, "cleanup_sweep", removed=removed_count)
        return removed_count

    async def _watchdog_loop(self) -> None:
        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self.WATCHDOG_INTERVAL_SEC)
                await self.run_health_check_once(send_alerts=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Watchdog loop error: %s", exc)

    async def check_proxy_reachable(self) -> bool:
        """Verify TCP reachability of the configured proxy host:port within 2 seconds."""
        proxy_url = self.settings.YT_PROXY_HTTP or self.settings.YT_PROXY_HTTPS
        if not proxy_url:
            return False
        try:
            parsed = urlparse(proxy_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 8080)
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        except Exception:
            return False

    def get_disk_and_workdir_stats_sync(self) -> tuple[int, float]:
        """Return (free_disk_mb, work_dir_size_mb)."""
        base = self.settings.WORK_DIR
        os.makedirs(base, exist_ok=True)
        usage = shutil.disk_usage(base)
        free_mb = int(usage.free // (1024 * 1024))

        total_bytes = 0
        for root, _, files in os.walk(base):
            for fname in files:
                fpath = os.path.join(root, fname)
                with contextlib.suppress(OSError):
                    total_bytes += os.path.getsize(fpath)
        work_mb = round(total_bytes / (1024 * 1024), 2)
        return free_mb, work_mb

    @property
    def active_jobs_count(self) -> int:
        return sum(p.active_count for p in self._pools.values())

    @property
    def total_queue_length(self) -> int:
        return sum(len(p.waiters) for p in self._pools.values())

    async def get_system_health_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of disk, workdir, jobs, queues, proxy, and Deno status."""
        free_mb, work_mb = await asyncio.to_thread(
            self.get_disk_and_workdir_stats_sync
        )
        proxy_configured = bool(
            self.settings.YT_PROXY_HTTP or self.settings.YT_PROXY_HTTPS
        )
        proxy_ok = (
            await self.check_proxy_reachable() if proxy_configured else False
        )
        deno_found, ejs_found = check_ytdlp_runtime_dependencies(log_result=False)

        return {
            "free_disk_mb": free_mb,
            "work_dir_size_mb": work_mb,
            "active_jobs": self.active_jobs_count,
            "queue_length": self.total_queue_length,
            "proxy_configured": proxy_configured,
            "proxy_reachable": proxy_ok,
            "deno_found": deno_found,
            "ejs_found": ejs_found,
        }

    async def _send_admin_alert_throttled(
        self, problem_key: str, text_fa: str
    ) -> None:
        if not self._bot:
            return
        now = time.monotonic()
        last_sent = self._last_alerts.get(problem_key, 0.0)
        if now - last_sent < self.ALERT_COOLDOWN_SEC:
            return
        self._last_alerts[problem_key] = now
        try:
            await self._bot.send_message(
                chat_id=self.settings.ADMIN_ID,
                text=f"🚨 <b>هشدار سلامت سرور:</b>\n{text_fa}",
            )
        except Exception as exc:
            logger.warning("Failed to send admin health alert: %s", exc)

    async def run_health_check_once(self, send_alerts: bool = True) -> dict[str, Any]:
        snap = await self.get_system_health_snapshot()
        logger.info(
            "Health watchdog | free_disk=%dMB work_dir=%.2fMB active_jobs=%d queue_len=%d proxy_reachable=%s deno_found=%s",
            snap["free_disk_mb"],
            snap["work_dir_size_mb"],
            snap["active_jobs"],
            snap["queue_length"],
            snap["proxy_reachable"],
            snap["deno_found"],
        )

        if send_alerts:
            if snap["free_disk_mb"] < self.settings.MIN_FREE_DISK_MB:
                await self._send_admin_alert_throttled(
                    "low_disk",
                    f"فضای خالی دیسک به <code>{snap['free_disk_mb']} MB</code> رسیده (کمتر از حد مجاز <code>{self.settings.MIN_FREE_DISK_MB} MB</code>).",
                )
            if snap["proxy_configured"] and not snap["proxy_reachable"]:
                await self._send_admin_alert_throttled(
                    "proxy_down",
                    "پروکسی یوتیوب (WARP/HTTP Proxy) در دسترس نیست و اتصال TCP برقرار نشد.",
                )

        return snap

    # ─────────────────────────────────────────────────────────────
    # Pre-flight Guards, FIFO Slot Acquisition & Per-User Limits
    # ─────────────────────────────────────────────────────────────

    def check_softsub_duration(
        self, duration_seconds: int | None, user_id: int | None = None
    ) -> None:
        """Refuse softsub video muxing if the video exceeds SOFTSUB_MAX_DURATION_SEC."""
        if (
            duration_seconds is not None
            and duration_seconds > self.settings.SOFTSUB_MAX_DURATION_SEC
        ):
            self._spawn_analytics(
                user_id, "job_rejected", reason="video_too_long"
            )
            max_mins = self.settings.SOFTSUB_MAX_DURATION_SEC // 60
            raise JobRejectedError(
                "video_too_long",
                f"این ویدئو بیشتر از {max_mins} دقیقه‌ست و ساخت سافت‌ساب براش فعال نیست؛ لطفاً از دکمه‌ی دانلود زیرنویس فارسی (.SRT) استفاده کن.",
            )

    async def check_free_disk(self, user_id: int | None = None) -> None:
        """Refuse downloads when free disk space is below MIN_FREE_DISK_MB."""
        free_mb, _ = await asyncio.to_thread(
            self.get_disk_and_workdir_stats_sync
        )
        if free_mb < self.settings.MIN_FREE_DISK_MB:
            self._spawn_analytics(user_id, "job_rejected", reason="low_disk")
            await self._send_admin_alert_throttled(
                "low_disk",
                f"فضای خالی دیسک به <code>{free_mb} MB</code> رسیده و دانلودهای جدید متوقف شدن.",
            )
            raise JobRejectedError(
                "low_disk",
                "فضای خالی سرور موقتا کمه، چند دقیقه دیگه دوباره امتحان کن.",
            )

    def _check_and_acquire_user_quota(self, user_id: int | None) -> None:
        if not user_id or user_id == self.settings.ADMIN_ID:
            return

        if user_id in self._active_users:
            self._spawn_analytics(user_id, "job_rejected", reason="user_busy")
            raise JobRejectedError(
                "user_busy",
                "الان یه درخواست سنگین در حال پردازش داری، صبر کن تموم بشه بعد دوباره امتحان کن.",
            )

        now = time.monotonic()
        history = self._user_hourly_history.setdefault(user_id, deque())
        while history and now - history[0] > 3600.0:
            history.popleft()

        if len(history) >= self.settings.USER_HEAVY_JOBS_PER_HOUR:
            self._spawn_analytics(
                user_id, "job_rejected", reason="user_rate_limit"
            )
            raise JobRejectedError(
                "user_rate_limit",
                f"تعداد درخواست‌هات تو یک ساعت گذشته به سقف مجاز ({self.settings.USER_HEAVY_JOBS_PER_HOUR} بار) رسیده، کمی بعد دوباره امتحان کن.",
            )

        history.append(now)
        self._active_users.add(user_id)

    def _release_user_quota(self, user_id: int | None) -> None:
        if user_id:
            self._active_users.discard(user_id)

    @contextlib.asynccontextmanager
    async def acquire_slot(
        self,
        kind: str,
        on_queue_position: QueuePositionCallback | None = None,
    ) -> AsyncIterator[None]:
        """Acquire a concurrency slot from a named pool ('video', 'gemini_url', 'translation') with FIFO queuing."""
        if self.is_shutting_down:
            raise JobRejectedError(
                "shutting_down",
                "ربات داره آپدیت می‌شه، چند ثانیه دیگه دوباره امتحان کن",
            )

        pool = self._pools[kind]
        user_id = current_user_id_var.get()

        if pool.active_count < pool.max_concurrent and not pool.waiters:
            pool.active_count += 1
        else:
            if len(pool.waiters) >= pool.max_queue_len:
                self._spawn_analytics(
                    user_id, "job_rejected", reason="queue_full", kind=kind
                )
                raise JobRejectedError(
                    "queue_full",
                    "الان سرور شلوغه، چند دقیقه دیگه امتحان کن",
                )

            my_event = asyncio.Event()
            pool.waiters.append(my_event)
            pos = len(pool.waiters)
            self._spawn_analytics(
                user_id, "job_queued", kind=kind, position=pos
            )
            if on_queue_position:
                with contextlib.suppress(Exception):
                    await on_queue_position(pos)

            try:
                await my_event.wait()
            except BaseException:
                if my_event in pool.waiters:
                    pool.waiters.remove(my_event)
                else:
                    self._wake_next_waiter(pool)
                raise

            if self.is_shutting_down:
                self._wake_next_waiter(pool)
                raise JobRejectedError(
                    "shutting_down",
                    "ربات داره آپدیت می‌شه، چند ثانیه دیگه دوباره امتحان کن",
                )

        try:
            yield
        finally:
            self._wake_next_waiter(pool)

    @staticmethod
    def _wake_next_waiter(pool: _FIFOQueuePool) -> None:
        while pool.waiters:
            next_ev = pool.waiters.popleft()
            if not next_ev.is_set():
                next_ev.set()
                return
        pool.active_count = max(0, pool.active_count - 1)

    @contextlib.asynccontextmanager
    async def run_heavy_job(
        self,
        user_id: int | None,
        kind: str,
        on_queue_position: QueuePositionCallback | None = None,
        on_shutdown_notify: ShutdownNotifyCallback | None = None,
        require_disk_check: bool = False,
    ) -> AsyncIterator[_ActiveHeavyJob]:
        """
        Full lifecycle guard for user-triggered heavy jobs (SRT, softsub, summary, web player):
        - Checks shutdown status, free disk space, and per-user rate/concurrency limits
        - Registers job for graceful shutdown notification
        - Acquires global FIFO concurrency slot
        """
        if self.is_shutting_down:
            self._spawn_analytics(
                user_id, "job_rejected", reason="shutting_down"
            )
            raise JobRejectedError(
                "shutting_down",
                "ربات داره آپدیت می‌شه، چند ثانیه دیگه دوباره امتحان کن",
            )

        if require_disk_check:
            await self.check_free_disk(user_id=user_id)

        self._check_and_acquire_user_quota(user_id)
        job_id = f"{kind}_{uuid.uuid4().hex[:10]}"
        job_record = _ActiveHeavyJob(
            job_id=job_id,
            user_id=user_id,
            kind=kind,
            task=asyncio.current_task(),
            notify_cb=on_shutdown_notify,
        )
        self._active_jobs[job_id] = job_record

        try:
            async with self.acquire_slot(
                kind, on_queue_position=on_queue_position
            ):
                yield job_record
        finally:
            self._active_jobs.pop(job_id, None)
            for dpath in list(job_record.work_dirs):
                shutil.rmtree(dpath, ignore_errors=True)
            self._release_user_quota(user_id)

    @contextlib.contextmanager
    def create_job_dir(self, prefix: str = "job_") -> Any:
        """
        Create an isolated subdirectory under settings.WORK_DIR and guarantee its removal
        in `finally` across success, exception, timeout, or task cancellation.
        """
        os.makedirs(self.settings.WORK_DIR, exist_ok=True)
        dir_name = f"{prefix}{int(time.time())}_{uuid.uuid4().hex[:8]}"
        full_path = os.path.abspath(
            os.path.join(self.settings.WORK_DIR, dir_name)
        )
        os.makedirs(full_path, exist_ok=True)

        # Attach to active heavy job if running inside one
        curr_task = asyncio.current_task()
        owning_job: _ActiveHeavyJob | None = None
        for job in self._active_jobs.values():
            if job.task is curr_task:
                owning_job = job
                job.work_dirs.add(full_path)
                break

        try:
            yield full_path
        finally:
            shutil.rmtree(full_path, ignore_errors=True)
            if owning_job is not None:
                owning_job.work_dirs.discard(full_path)

    # ─────────────────────────────────────────────────────────────
    # Subprocess Hygiene & Graceful Shutdown
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    async def run_subprocess_safe(
        cmd: list[str],
        timeout: float = 45.0,
    ) -> tuple[int, bytes, bytes]:
        """
        Execute a child subprocess with strict cleanup: if timed out or cancelled,
        kills the child process and awaits its termination so no zombie/orphan remains.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return proc.returncode or 0, stdout, stderr
        except (asyncio.TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(ProcessLookupError, Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise

    async def shutdown(self) -> None:
        """
        Graceful shutdown handler on SIGTERM/SIGINT:
        - Stop accepting new heavy jobs
        - Wait up to 20 seconds for running heavy jobs to finish
        - Cancel any remaining jobs, clean their work directories, and notify affected users
        """
        if self.is_shutting_down:
            return
        self.is_shutting_down = True
        logger.info(
            "Graceful shutdown initiated: active_jobs=%d",
            len(self._active_jobs),
        )

        # Wake any queued waiters so they abort immediately
        for pool in self._pools.values():
            while pool.waiters:
                ev = pool.waiters.popleft()
                ev.set()

        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

        # Wait up to 20 seconds for running heavy jobs to finish naturally
        deadline = time.monotonic() + 20.0
        while self._active_jobs and time.monotonic() < deadline:
            await asyncio.sleep(0.5)

        remaining_jobs = list(self._active_jobs.values())
        if remaining_jobs:
            logger.warning(
                "Cancelling %d remaining heavy job(s) after 20s shutdown grace period.",
                len(remaining_jobs),
            )
            shutdown_msg = "ربات داره آپدیت می‌شه، چند ثانیه دیگه دوباره امتحان کن"
            for job in remaining_jobs:
                if job.notify_cb:
                    with contextlib.suppress(Exception):
                        await job.notify_cb(shutdown_msg)
                if job.task and not job.task.done():
                    job.task.cancel()
                for dpath in list(job.work_dirs):
                    shutil.rmtree(dpath, ignore_errors=True)
