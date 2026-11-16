import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from config import settings
from services.analytics import Analytics
from services.i18n import t
from services.job_manager import JobManager
from services.llm_router import LLMRouter

logger = logging.getLogger(__name__)
stats_router = Router(name="stats_and_version_router")

PROCESS_START_UTC = datetime.now(timezone.utc)
PROCESS_START_MONOTONIC = time.monotonic()


async def _run_git_cmd(args: list[str]) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode == 0 and stdout:
            return stdout.decode("utf-8", errors="ignore").strip()
    except Exception:
        pass
    return "unknown"


def _format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m {secs}s")
    return " ".join(parts)


async def get_version_info(
    llm_router: LLMRouter,
    job_manager: JobManager | None = None,
    analytics: Analytics | None = None,
) -> dict[str, str]:
    commit_hash, commit_date = await asyncio.gather(
        _run_git_cmd(["git", "rev-parse", "--short", "HEAD"]),
        _run_git_cmd(["git", "log", "-1", "--format=%ci"]),
    )
    uptime_str = _format_uptime(time.monotonic() - PROCESS_START_MONOTONIC)
    start_str = PROCESS_START_UTC.strftime("%Y-%m-%d %H:%M:%S UTC")
    open_circuits = llm_router.get_open_circuits()

    if job_manager is not None:
        health = await job_manager.get_system_health_snapshot()
    else:
        health = {
            "free_disk_mb": 0,
            "work_dir_size_mb": 0.0,
            "active_jobs": 0,
            "queue_length": 0,
            "proxy_reachable": False,
            "deno_found": False,
        }

    gemini_sec_today = (
        await analytics.get_gemini_url_seconds_today() if analytics else 0
    )
    supa_credits_month = (
        await analytics.get_supadata_credits_this_month() if analytics else 0
    )

    return {
        "commit": commit_hash,
        "commit_date": commit_date,
        "started_utc": start_str,
        "uptime": uptime_str,
        "fast_model": llm_router.active_fast_model,
        "open_circuits": ", ".join(open_circuits) if open_circuits else "none",
        "openrouter_enabled": "yes" if llm_router.openrouter_enabled else "no",
        "free_disk_mb": str(health["free_disk_mb"]),
        "work_dir_size_mb": f"{health['work_dir_size_mb']:.2f}",
        "active_jobs": str(health["active_jobs"]),
        "queue_length": str(health["queue_length"]),
        "proxy_reachable": "yes" if health["proxy_reachable"] else "no",
        "deno_found": "yes" if health["deno_found"] else "no",
        "gemini_url_today": f"{gemini_sec_today}s / {settings.GEMINI_URL_DAILY_LIMIT_SEC}s",
        "supadata_month": f"{supa_credits_month} / {settings.SUPADATA_MONTHLY_LIMIT}",
    }


@stats_router.message(Command("version"))
async def cmd_version(
    message: Message,
    llm_router: LLMRouter,
    job_manager: JobManager,
    analytics: Analytics,
) -> None:
    """Answer /version for any user in their active language."""
    info = await get_version_info(llm_router, job_manager, analytics)
    text = t(
        "version_message",
        commit=html.escape(info["commit"]),
        commit_date=html.escape(info["commit_date"]),
        started_utc=html.escape(info["started_utc"]),
        uptime=html.escape(info["uptime"]),
        fast_model=html.escape(info["fast_model"]),
        open_circuits=html.escape(info["open_circuits"]),
        openrouter_enabled=info["openrouter_enabled"],
        free_disk_mb=info["free_disk_mb"],
        work_dir_size_mb=info["work_dir_size_mb"],
        active_jobs=info["active_jobs"],
        queue_length=info["queue_length"],
        proxy_reachable=info["proxy_reachable"],
        deno_found=info["deno_found"],
        gemini_url_today=info["gemini_url_today"],
        supadata_month=info["supadata_month"],
    )
    await message.reply(text)


def _parse_days_arg(command: CommandObject | None) -> int:
    if command and command.args:
        raw = command.args.strip().split()[0]
        if raw.isdigit():
            return min(365, max(1, int(raw)))
    return 7


@stats_router.message(Command("stats"))
async def cmd_stats(
    message: Message,
    analytics: Analytics,
    job_manager: JobManager,
    command: CommandObject | None = None,
) -> None:
    """Answer /stats ONLY for settings.ADMIN_ID; stay completely silent otherwise."""
    if not message.from_user or message.from_user.id != settings.ADMIN_ID:
        return

    days = _parse_days_arg(command)
    report = await analytics.get_report(days=days)
    health = await job_manager.get_system_health_snapshot()

    u = report["unique_users"]
    s = report["searches"]
    av = report["audio_verification"]
    llm = report["llm"]
    tr = report["transcripts"]

    lines: list[str] = [
        t(
            "stats_header_block",
            days=days,
            u_today=u["today"],
            u_7d=u["last_7_days"],
            u_all=u["all_time"],
            s_total=s["total"],
            s_found=s["found"],
            s_rate=s["success_rate_pct"],
            s_conf=s["avg_confidence_pct"],
            av_used=av["used"],
            av_skipped=av["skipped"],
            av_timeout=av["timed_out"],
            av_en=av["english"],
            av_dub=av["dubbed"],
            free_mb=health["free_disk_mb"],
            work_mb=health["work_dir_size_mb"],
            active_jobs=health["active_jobs"],
            queue_len=health["queue_length"],
            proxy_ok="yes" if health["proxy_reachable"] else "no",
            deno_ok="yes" if health["deno_found"] else "no",
            gem_used=tr["gemini_url_quota_today_sec"],
            gem_limit=tr["gemini_url_daily_limit_sec"],
            supa_used=tr["supadata_credits_month"],
            supa_limit=tr["supadata_monthly_limit"],
        )
    ]

    for src_name, src_data in tr.get("sources", {}).items():
        lines.append(
            f"  - <code>{html.escape(src_name)}</code>: ok={src_data['ok']}, fail={src_data['failed']}"
        )

    if report["actions"]:
        lines.append(t("stats_sec_actions"))
        for act_name, act_data in report["actions"].items():
            lines.append(
                f"• <code>{html.escape(act_name)}</code>: "
                f"ok=<code>{act_data['ok']}</code> fail=<code>{act_data['failed']}</code> "
                f"(avg={act_data['avg_sec']}s, p95={act_data['p95_sec']}s)"
            )

    if report["pipeline_steps"]:
        lines.append(t("stats_sec_steps"))
        for st_name, st_data in report["pipeline_steps"].items():
            lines.append(
                f"• <code>{html.escape(st_name)}</code>: "
                f"avg=<code>{st_data['avg']}s</code> p95=<code>{st_data['p95']}s</code> "
                f"(ok={st_data['ok']}, to={st_data['timeout']}, err={st_data['failed']})"
            )

    lines.append(
        t(
            "stats_sec_llm",
            total=llm["total_answered"],
            switches=llm["switches"],
            or_today=llm["openrouter_fallbacks_today"],
            all_failed=llm["all_providers_failed"],
        )
    )
    for m_name, m_data in list(llm["models"].items())[:5]:
        lines.append(
            f"  - <code>{html.escape(m_name)}</code>: {m_data['share_pct']}% (ok={m_data['ok']}, avg={m_data['avg_sec']}s)"
        )

    if report["errors_and_timeouts"]:
        lines.append(t("stats_sec_errors"))
        for err_key, cnt in list(report["errors_and_timeouts"].items())[:6]:
            lines.append(f"• <code>{html.escape(err_key)}</code>: <code>{cnt}</code>")

    if report["top_returning_users"]:
        lines.append(t("stats_sec_top_users"))
        for u_item in report["top_returning_users"]:
            lines.append(
                t(
                    "stats_top_user_row",
                    hash_prefix=u_item["hash_prefix"],
                    searches=u_item["searches"],
                    actions=u_item["actions"],
                )
            )

    full_text = "\n".join(lines)
    if len(full_text) > 3450:
        full_text = full_text[:3420] + "\n..."

    await message.reply(full_text)


@stats_router.message(Command("stats_export"))
async def cmd_stats_export(
    message: Message,
    analytics: Analytics,
    job_manager: JobManager,
    command: CommandObject | None = None,
) -> None:
    """Send the complete analytics report as a JSON file ONLY to settings.ADMIN_ID."""
    if not message.from_user or message.from_user.id != settings.ADMIN_ID:
        return

    days = _parse_days_arg(command)
    report = await analytics.get_report(days=days)
    report["system_health"] = await job_manager.get_system_health_snapshot()
    json_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"vsf_stats_{days}d_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    doc = BufferedInputFile(file=json_bytes, filename=filename)
    await message.reply_document(
        document=doc,
        caption=t("stats_export_caption", days=days),
    )
