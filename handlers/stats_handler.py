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
    """Answer /version for any user with build, model, and resource health metadata."""
    info = await get_version_info(llm_router, job_manager, analytics)
    text = (
        "🤖 <b>وضعیت و نسخه‌ی ربات:</b>\n\n"
        f"• <b>کامیت (Git):</b> <code>{html.escape(info['commit'])}</code>\n"
        f"• <b>تاریخ کامیت:</b> <code>{html.escape(info['commit_date'])}</code>\n"
        f"• <b>زمان راه‌اندازی:</b> <code>{html.escape(info['started_utc'])}</code>\n"
        f"• <b>آپ‌تایم:</b> <code>{html.escape(info['uptime'])}</code>\n"
        f"• <b>مدل سریع فعال:</b> <code>{html.escape(info['fast_model'])}</code>\n"
        f"• <b>مدارهای باز (Circuit Breaker):</b> <code>{html.escape(info['open_circuits'])}</code>\n"
        f"• <b>پشتیبان OpenRouter:</b> <code>{info['openrouter_enabled']}</code>\n"
        f"• <b>فضای خالی دیسک:</b> <code>{info['free_disk_mb']} MB</code>\n"
        f"• <b>حجم پوشه کاری:</b> <code>{info['work_dir_size_mb']} MB</code>\n"
        f"• <b>پردازش‌های فعال / صف:</b> <code>{info['active_jobs']} active / {info['queue_length']} queued</code>\n"
        f"• <b>اتصال پروکسی یوتیوب:</b> <code>{info['proxy_reachable']}</code>\n"
        f"• <b>نصب بودن Deno:</b> <code>{info['deno_found']}</code>\n"
        f"• <b>مصرف امروز Gemini-URL:</b> <code>{info['gemini_url_today']}</code>\n"
        f"• <b>مصرف ماهانه Supadata:</b> <code>{info['supadata_month']}</code>"
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
        f"📊 <b>گزارش آماری ربات ({days} روز گذشته)</b>\n",
        "👥 <b>کاربران یکتا:</b>",
        f"• امروز: <code>{u['today']}</code> | ۷ روز: <code>{u['last_7_days']}</code> | کل: <code>{u['all_time']}</code>\n",
        "🔍 <b>جستجوها:</b>",
        f"• کل: <code>{s['total']}</code> | موفق: <code>{s['found']}</code> (<code>{s['success_rate_pct']}%</code>)",
        f"• میانگین اطمینان: <code>{s['avg_confidence_pct']}%</code>\n",
        "🎙 <b>تایید صوتی (Whisper):</b>",
        f"• استفاده شده: <code>{av['used']}</code> | رد شده: <code>{av['skipped']}</code> | تایم‌اوت: <code>{av['timed_out']}</code>",
        f"• زبان صدا: انگلیسی <code>{av['english']}</code> | دوبله <code>{av['dubbed']}</code>\n",
        "🖥 <b>وضعیت منابع سرور:</b>",
        f"• دیسک آزاد: <code>{health['free_disk_mb']} MB</code> | پوشه کاری: <code>{health['work_dir_size_mb']:.2f} MB</code>",
        f"• کارهای فعال: <code>{health['active_jobs']}</code> | صف: <code>{health['queue_length']}</code>",
        f"• پروکسی فعال: <code>{'yes' if health['proxy_reachable'] else 'no'}</code> | Deno: <code>{'yes' if health['deno_found'] else 'no'}</code>\n",
        "📜 <b>منابع زیرنویس و سهمیه‌ها:</b>",
        f"• مصرف امروز ویدئوی جمنای: <code>{tr['gemini_url_quota_today_sec']}s / {tr['gemini_url_daily_limit_sec']}s</code>",
        f"• مصرف ماهانه Supadata: <code>{tr['supadata_credits_month']} / {tr['supadata_monthly_limit']}</code>",
    ]

    for src_name, src_data in tr.get("sources", {}).items():
        lines.append(
            f"  - <code>{html.escape(src_name)}</code>: ok={src_data['ok']}, fail={src_data['failed']}"
        )

    if report["actions"]:
        lines.append("\n🎛 <b>عملکرد دکمه‌ها:</b>")
        for act_name, act_data in report["actions"].items():
            lines.append(
                f"• <code>{html.escape(act_name)}</code>: "
                f"ok=<code>{act_data['ok']}</code> fail=<code>{act_data['failed']}</code> "
                f"(avg={act_data['avg_sec']}s, p95={act_data['p95_sec']}s)"
            )

    if report["pipeline_steps"]:
        lines.append("\n⏱ <b>مراحل پایپ‌لاین:</b>")
        for st_name, st_data in report["pipeline_steps"].items():
            lines.append(
                f"• <code>{html.escape(st_name)}</code>: "
                f"avg=<code>{st_data['avg']}s</code> p95=<code>{st_data['p95']}s</code> "
                f"(ok={st_data['ok']}, to={st_data['timeout']}, err={st_data['failed']})"
            )

    lines.append("\n🧠 <b>مدل‌های هوش مصنوعی:</b>")
    lines.append(
        f"• کل پاسخ‌ها: <code>{llm['total_answered']}</code> | سوییچ‌ها: <code>{llm['switches']}</code>"
    )
    lines.append(
        f"• فال‌بک به OpenRouter امروز: <code>{llm['openrouter_fallbacks_today']}</code> | شکست کامل: <code>{llm['all_providers_failed']}</code>"
    )
    for m_name, m_data in list(llm["models"].items())[:5]:
        lines.append(
            f"  - <code>{html.escape(m_name)}</code>: {m_data['share_pct']}% (ok={m_data['ok']}, avg={m_data['avg_sec']}s)"
        )

    if report["errors_and_timeouts"]:
        lines.append("\n⚠️ <b>خطاها و تایم‌اوت‌ها:</b>")
        for err_key, cnt in list(report["errors_and_timeouts"].items())[:6]:
            lines.append(f"• <code>{html.escape(err_key)}</code>: <code>{cnt}</code>")

    if report["top_returning_users"]:
        lines.append("\n🏆 <b>فعال‌ترین کاربران (Hash Prefix):</b>")
        for u_item in report["top_returning_users"]:
            lines.append(
                f"• <code>{u_item['hash_prefix']}</code>: "
                f"جستجو={u_item['searches']} | اکشن={u_item['actions']}"
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
        caption=f"📁 خروجی کامل آمار ربات (بازه {days} روزه)",
    )
