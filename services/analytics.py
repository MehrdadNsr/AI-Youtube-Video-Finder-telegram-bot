"""
Privacy-Preserving Usage Analytics Service.

WHAT IS STORED:
- `id`: Auto-incrementing integer primary key.
- `ts`: UTC ISO-8601 timestamp of the event.
- `user_hash`: 16-character hex prefix of HMAC-SHA256(user_id, ANALYTICS_SALT).
- `event_type`: Short event identifier (e.g., 'update', 'step_done', 'search_done',
  'search_failed', 'action_done', 'action_failed', 'llm_call', 'llm_switch',
  'llm_all_failed', 'transcript_fetch', 'transcript_fallback_used', 'job_queued',
  'job_rejected', 'cleanup_sweep').
- `props_json`: Compact JSON (<= 1 KB) containing operational metrics only (durations,
  status codes, boolean flags, confidence scores, model IDs, candidate counts,
  processed video seconds, queue positions, exception class names).
- `gemini_video_quota`: Daily UTC counter of processed YouTube video seconds via Gemini URL input.
- `supadata_monthly_quota`: Monthly UTC counter of consumed Supadata native transcript credits.

WHAT IS NOT STORED:
- Never stores raw Telegram `user_id`, `chat_id`, username, first/last name, or profile data.
- Never stores message text, captions, extracted entities, search queries, or URLs.
- Never stores audio bytes, Whisper transcripts, YouTube transcripts, or video content.
- Never stores API keys, proxy credentials, tokens, or raw exception message strings.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


class Analytics:
    """Asynchronous SQLite-backed usage analytics collector, quota tracker, and reporter."""

    def __init__(
        self,
        db_path: str,
        salt: str,
        confidence_threshold: float = 0.45,
        gemini_url_daily_limit_sec: int = 25200,
        supadata_monthly_limit: int = 90,
    ) -> None:
        self.db_path = db_path
        self._salt_bytes = salt.encode("utf-8")
        self.confidence_threshold = confidence_threshold
        self.gemini_url_daily_limit_sec = gemini_url_daily_limit_sec
        self.supadata_monthly_limit = supadata_monthly_limit
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def hash_user(self, user_id: int | None) -> str:
        """Compute a 16-hex-character HMAC-SHA256 hash for a numeric user ID."""
        if not user_id:
            return "anonymous0000000"
        digest = hmac.new(
            self._salt_bytes,
            str(user_id).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest[:16]

    def _init_db_sync(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    user_hash TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    props_json TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_hash);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gemini_video_quota (
                    utc_date TEXT PRIMARY KEY,
                    used_seconds INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supadata_monthly_quota (
                    utc_month TEXT PRIMARY KEY,
                    used_credits INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.commit()

    async def ensure_initialized(self) -> None:
        """Initialize the SQLite database and WAL mode if not already done."""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            try:
                await asyncio.to_thread(self._init_db_sync)
                self._initialized = True
            except Exception as exc:
                logger.warning("Failed to initialize analytics DB: %s", exc)

    def _insert_event_sync(
        self, ts: str, user_hash: str, event_type: str, props_json: str
    ) -> None:
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute(
                "INSERT INTO events (ts, user_hash, event_type, props_json) VALUES (?, ?, ?, ?)",
                (ts, user_hash, event_type, props_json),
            )
            conn.commit()

    async def log_event(
        self, user_id: int | None, event_type: str, **props: Any
    ) -> None:
        """
        Record an analytics event without ever raising or blocking the caller.
        Automatically enforces the <= 1 KB limit on props_json.
        """
        try:
            await self.ensure_initialized()
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            user_hash = self.hash_user(user_id)
            raw_json = json.dumps(props, ensure_ascii=False, default=str)
            if len(raw_json.encode("utf-8")) > 1024:
                trimmed: dict[str, Any] = {}
                for k, v in props.items():
                    if isinstance(v, str) and len(v) > 64:
                        trimmed[k] = v[:64]
                    else:
                        trimmed[k] = v
                raw_json = json.dumps(trimmed, ensure_ascii=False, default=str)
                if len(raw_json.encode("utf-8")) > 1024:
                    raw_json = "{}"

            await asyncio.to_thread(
                self._insert_event_sync, ts, user_hash, event_type, raw_json
            )
        except Exception as exc:
            logger.warning("Analytics log_event failed (%s): %s", event_type, exc)

    def _get_gemini_url_seconds_today_sync(self) -> int:
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT used_seconds FROM gemini_video_quota WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            return int(row[0]) if row else 0

    def _add_gemini_url_seconds_sync(self, seconds: int) -> int:
        utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        inc = max(0, int(seconds))
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute(
                """
                INSERT INTO gemini_video_quota (utc_date, used_seconds)
                VALUES (?, ?)
                ON CONFLICT(utc_date) DO UPDATE SET used_seconds = used_seconds + excluded.used_seconds;
                """,
                (utc_date, inc),
            )
            conn.commit()
            row = conn.execute(
                "SELECT used_seconds FROM gemini_video_quota WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            return int(row[0]) if row else inc

    async def get_gemini_url_seconds_today(self) -> int:
        """Return total YouTube video seconds sent to Gemini URL input in the current UTC day."""
        try:
            await self.ensure_initialized()
            return await asyncio.to_thread(self._get_gemini_url_seconds_today_sync)
        except Exception as exc:
            logger.warning("Failed to read Gemini URL daily quota: %s", exc)
            return 0

    async def add_gemini_url_seconds(self, seconds: int) -> int:
        """Increment today's processed YouTube video seconds for Gemini URL input and return new total."""
        try:
            await self.ensure_initialized()
            return await asyncio.to_thread(
                self._add_gemini_url_seconds_sync, seconds
            )
        except Exception as exc:
            logger.warning("Failed to increment Gemini URL daily quota: %s", exc)
            return 0

    def _get_supadata_credits_month_sync(self) -> int:
        utc_month = datetime.now(timezone.utc).strftime("%Y-%m")
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT used_credits FROM supadata_monthly_quota WHERE utc_month = ?",
                (utc_month,),
            ).fetchone()
            return int(row[0]) if row else 0

    def _add_supadata_credits_month_sync(self, credits_used: int = 1) -> int:
        utc_month = datetime.now(timezone.utc).strftime("%Y-%m")
        inc = max(0, int(credits_used))
        with sqlite3.connect(self.db_path, timeout=5.0) as conn:
            conn.execute(
                """
                INSERT INTO supadata_monthly_quota (utc_month, used_credits)
                VALUES (?, ?)
                ON CONFLICT(utc_month) DO UPDATE SET used_credits = used_credits + excluded.used_credits;
                """,
                (utc_month, inc),
            )
            conn.commit()
            row = conn.execute(
                "SELECT used_credits FROM supadata_monthly_quota WHERE utc_month = ?",
                (utc_month,),
            ).fetchone()
            return int(row[0]) if row else inc

    async def get_supadata_credits_this_month(self) -> int:
        """Return total Supadata credits consumed in the current UTC month."""
        try:
            await self.ensure_initialized()
            return await asyncio.to_thread(self._get_supadata_credits_month_sync)
        except Exception as exc:
            logger.warning("Failed to read Supadata monthly quota: %s", exc)
            return 0

    async def add_supadata_credit(self, credits_used: int = 1) -> int:
        """Increment this UTC month's Supadata credits and return the new total."""
        try:
            await self.ensure_initialized()
            return await asyncio.to_thread(
                self._add_supadata_credits_month_sync, credits_used
            )
        except Exception as exc:
            logger.warning("Failed to increment Supadata monthly quota: %s", exc)
            return 0

    @staticmethod
    def _calc_avg_p95(values: list[float]) -> dict[str, float | int]:
        if not values:
            return {"count": 0, "avg": 0.0, "p95": 0.0}
        sorted_vals = sorted(values)
        count = len(sorted_vals)
        avg = sum(sorted_vals) / count
        idx_95 = min(count - 1, max(0, math.ceil(0.95 * count) - 1))
        return {
            "count": count,
            "avg": round(avg, 2),
            "p95": round(sorted_vals[idx_95], 2),
        }

    def _build_report_sync(self, days: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
            timespec="seconds"
        )
        utc_date = now.strftime("%Y-%m-%d")
        utc_month = now.strftime("%Y-%m")
        seven_days_cutoff = (now - timedelta(days=7)).isoformat(timespec="seconds")
        window_cutoff = (now - timedelta(days=max(1, days))).isoformat(
            timespec="seconds"
        )

        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row

            u_today = conn.execute(
                "SELECT COUNT(DISTINCT user_hash) FROM events WHERE ts >= ? AND user_hash != 'anonymous0000000'",
                (today_start,),
            ).fetchone()[0]
            u_7d = conn.execute(
                "SELECT COUNT(DISTINCT user_hash) FROM events WHERE ts >= ? AND user_hash != 'anonymous0000000'",
                (seven_days_cutoff,),
            ).fetchone()[0]
            u_window = conn.execute(
                "SELECT COUNT(DISTINCT user_hash) FROM events WHERE ts >= ? AND user_hash != 'anonymous0000000'",
                (window_cutoff,),
            ).fetchone()[0]
            u_all = conn.execute(
                "SELECT COUNT(DISTINCT user_hash) FROM events WHERE user_hash != 'anonymous0000000'"
            ).fetchone()[0]

            quota_row = conn.execute(
                "SELECT used_seconds FROM gemini_video_quota WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            gemini_video_sec_today = int(quota_row[0]) if quota_row else 0

            supa_row = conn.execute(
                "SELECT used_credits FROM supadata_monthly_quota WHERE utc_month = ?",
                (utc_month,),
            ).fetchone()
            supadata_credits_month = int(supa_row[0]) if supa_row else 0

            rows = conn.execute(
                "SELECT ts, user_hash, event_type, props_json FROM events WHERE ts >= ? ORDER BY id ASC",
                (window_cutoff,),
            ).fetchall()

        total_searches = 0
        found_searches = 0
        confidences: list[float] = []
        audio_used = 0
        audio_skipped = 0
        audio_timed_out = 0
        dubbed_count = 0
        english_audio_count = 0

        button_counts: dict[str, dict[str, int]] = {}
        action_durations: dict[str, list[float]] = {}
        step_durations: dict[str, list[float]] = {}
        step_statuses: dict[str, dict[str, int]] = {}
        errors_and_timeouts: dict[str, int] = {}

        llm_models: dict[str, dict[str, Any]] = {}
        total_llm_ok = 0
        openrouter_today = 0
        all_providers_failed = 0
        llm_switches_count = 0

        transcript_sources: dict[str, dict[str, int]] = {}
        transcript_fallbacks = 0
        jobs_queued = 0
        jobs_rejected: dict[str, int] = {}

        user_activity: dict[str, dict[str, int]] = {}

        for row in rows:
            ts = str(row["ts"])
            u_hash = str(row["user_hash"])
            ev_type = str(row["event_type"])
            try:
                props = json.loads(row["props_json"]) if row["props_json"] else {}
            except Exception:
                props = {}

            if u_hash != "anonymous0000000":
                ua = user_activity.setdefault(
                    u_hash, {"events": 0, "searches": 0, "actions": 0}
                )
                ua["events"] += 1

            if ev_type == "step_done":
                step_name = str(props.get("step", "unknown"))
                status = str(props.get("status", "ok"))
                secs = float(props.get("seconds", 0.0) or 0.0)
                step_durations.setdefault(step_name, []).append(secs)
                st_map = step_statuses.setdefault(
                    step_name, {"ok": 0, "timeout": 0, "failed": 0}
                )
                st_map[status] = st_map.get(status, 0) + 1

                if step_name in ("audio", "whisper", "match") and status == "timeout":
                    audio_timed_out += 1
                if status in ("timeout", "failed"):
                    key = f"step:{step_name}:{status}"
                    errors_and_timeouts[key] = errors_and_timeouts.get(key, 0) + 1

            elif ev_type == "search_done":
                total_searches += 1
                if u_hash != "anonymous0000000":
                    user_activity[u_hash]["searches"] += 1

                conf = float(props.get("confidence", 0.0) or 0.0)
                confidences.append(conf)
                is_found = bool(
                    props.get("found", conf >= self.confidence_threshold)
                )
                if is_found:
                    found_searches += 1

                has_audio = bool(props.get("has_audio", False))
                audio_match = props.get("audio_match")
                is_dubbed = bool(props.get("dubbed", False))

                if has_audio and audio_match is not None:
                    audio_used += 1
                    if is_dubbed:
                        dubbed_count += 1
                    else:
                        english_audio_count += 1
                elif not has_audio:
                    audio_skipped += 1

            elif ev_type == "search_failed":
                total_searches += 1
                if u_hash != "anonymous0000000":
                    user_activity[u_hash]["searches"] += 1
                reason = str(props.get("reason", "unknown"))
                key = f"search_failed:{reason}"
                errors_and_timeouts[key] = errors_and_timeouts.get(key, 0) + 1

            elif ev_type == "action_done":
                action = str(props.get("action", "unknown"))
                secs = float(props.get("seconds", 0.0) or 0.0)
                ok = bool(props.get("ok", True))
                if u_hash != "anonymous0000000":
                    user_activity[u_hash]["actions"] += 1
                b_entry = button_counts.setdefault(action, {"ok": 0, "failed": 0})
                if ok:
                    b_entry["ok"] += 1
                    action_durations.setdefault(action, []).append(secs)
                else:
                    b_entry["failed"] += 1

            elif ev_type == "action_failed":
                action = str(props.get("action", "unknown"))
                err_type = str(props.get("error_type", "Error"))
                if u_hash != "anonymous0000000":
                    user_activity[u_hash]["actions"] += 1
                b_entry = button_counts.setdefault(action, {"ok": 0, "failed": 0})
                b_entry["failed"] += 1
                key = f"action:{action}:{err_type}"
                errors_and_timeouts[key] = errors_and_timeouts.get(key, 0) + 1

            elif ev_type == "llm_call":
                model = str(props.get("model", "unknown"))
                provider = str(props.get("provider", "gemini"))
                secs = float(props.get("seconds", 0.0) or 0.0)
                ok = bool(props.get("ok", False))
                m_entry = llm_models.setdefault(
                    model,
                    {"provider": provider, "ok": 0, "failed": 0, "durations": []},
                )
                if ok:
                    m_entry["ok"] += 1
                    m_entry["durations"].append(secs)
                    total_llm_ok += 1
                    if provider == "openrouter" and ts >= today_start:
                        openrouter_today += 1
                else:
                    m_entry["failed"] += 1

            elif ev_type == "llm_switch":
                llm_switches_count += 1
                reason = str(props.get("reason", "unknown"))
                from_m = str(props.get("from_model", "?"))
                key = f"llm_switch:{from_m}:{reason}"
                errors_and_timeouts[key] = errors_and_timeouts.get(key, 0) + 1

            elif ev_type == "llm_all_failed":
                all_providers_failed += 1
                errors_and_timeouts["llm_all_failed"] = (
                    errors_and_timeouts.get("llm_all_failed", 0) + 1
                )

            elif ev_type == "transcript_fetch":
                src = str(props.get("source", "unknown"))
                ok = bool(props.get("ok", False))
                err_kind = props.get("error_kind")
                ts_entry = transcript_sources.setdefault(
                    src, {"ok": 0, "failed": 0}
                )
                if ok:
                    ts_entry["ok"] += 1
                else:
                    ts_entry["failed"] += 1
                    if err_kind:
                        key = f"transcript:{src}:{err_kind}"
                        errors_and_timeouts[key] = (
                            errors_and_timeouts.get(key, 0) + 1
                        )

            elif ev_type == "transcript_fallback_used":
                transcript_fallbacks += 1

            elif ev_type == "job_queued":
                jobs_queued += 1

            elif ev_type == "job_rejected":
                r_reason = str(props.get("reason", "unknown"))
                jobs_rejected[r_reason] = jobs_rejected.get(r_reason, 0) + 1
                key = f"job_rejected:{r_reason}"
                errors_and_timeouts[key] = errors_and_timeouts.get(key, 0) + 1

        success_rate = (
            round((found_searches / total_searches) * 100, 1)
            if total_searches > 0
            else 0.0
        )
        avg_confidence = (
            round((sum(confidences) / len(confidences)) * 100, 1)
            if confidences
            else 0.0
        )

        step_stats: dict[str, Any] = {}
        for step_name, vals in step_durations.items():
            metrics = self._calc_avg_p95(vals)
            st_counts = step_statuses.get(step_name, {})
            step_stats[step_name] = {
                **metrics,
                "ok": st_counts.get("ok", 0),
                "timeout": st_counts.get("timeout", 0),
                "failed": st_counts.get("failed", 0),
            }

        action_stats: dict[str, Any] = {}
        all_actions = set(button_counts.keys()).union(action_durations.keys())
        for act in sorted(all_actions):
            metrics = self._calc_avg_p95(action_durations.get(act, []))
            counts = button_counts.get(act, {"ok": 0, "failed": 0})
            action_stats[act] = {
                "ok": counts.get("ok", 0),
                "failed": counts.get("failed", 0),
                "avg_sec": metrics["avg"],
                "p95_sec": metrics["p95"],
            }

        llm_share: dict[str, Any] = {}
        for model_name, info in sorted(
            llm_models.items(), key=lambda x: x[1]["ok"], reverse=True
        ):
            ok_cnt = info["ok"]
            share = (
                round((ok_cnt / total_llm_ok) * 100, 1)
                if total_llm_ok > 0
                else 0.0
            )
            d_stats = self._calc_avg_p95(info["durations"])
            llm_share[model_name] = {
                "provider": info["provider"],
                "ok": ok_cnt,
                "failed": info["failed"],
                "share_pct": share,
                "avg_sec": d_stats["avg"],
                "p95_sec": d_stats["p95"],
            }

        top_users = sorted(
            user_activity.items(),
            key=lambda kv: (kv[1]["searches"] + kv[1]["actions"], kv[1]["events"]),
            reverse=True,
        )[:5]
        top_returning = [
            {
                "hash_prefix": u_hash[:8],
                "searches": data["searches"],
                "actions": data["actions"],
                "events": data["events"],
            }
            for u_hash, data in top_users
        ]

        return {
            "window_days": days,
            "generated_at_utc": now.isoformat(timespec="seconds"),
            "unique_users": {
                "today": u_today,
                "last_7_days": u_7d,
                "window": u_window,
                "all_time": u_all,
            },
            "searches": {
                "total": total_searches,
                "found": found_searches,
                "success_rate_pct": success_rate,
                "avg_confidence_pct": avg_confidence,
            },
            "audio_verification": {
                "used": audio_used,
                "skipped": audio_skipped,
                "timed_out": audio_timed_out,
                "dubbed": dubbed_count,
                "english": english_audio_count,
            },
            "transcripts": {
                "sources": transcript_sources,
                "fallbacks_used": transcript_fallbacks,
                "gemini_url_quota_today_sec": gemini_video_sec_today,
                "gemini_url_daily_limit_sec": self.gemini_url_daily_limit_sec,
                "supadata_credits_month": supadata_credits_month,
                "supadata_monthly_limit": self.supadata_monthly_limit,
            },
            "jobs": {
                "queued": jobs_queued,
                "rejected": jobs_rejected,
            },
            "actions": action_stats,
            "pipeline_steps": step_stats,
            "llm": {
                "total_answered": total_llm_ok,
                "switches": llm_switches_count,
                "openrouter_fallbacks_today": openrouter_today,
                "all_providers_failed": all_providers_failed,
                "models": llm_share,
            },
            "errors_and_timeouts": dict(
                sorted(
                    errors_and_timeouts.items(), key=lambda x: x[1], reverse=True
                )[:15]
            ),
            "top_returning_users": top_returning,
        }

    async def get_report(self, days: int = 7) -> dict[str, Any]:
        """Generate a structured usage report for the past `days` days."""
        await self.ensure_initialized()
        return await asyncio.to_thread(self._build_report_sync, max(1, days))
