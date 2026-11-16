"""
Telegram Menu Button command lists.

The menu (the "Menu" button next to the message box) is registered per language:
- default list (no language_code) in English, shown to every non-Persian Telegram client
- language_code="fa" list in Persian
This mirrors resolve_user_lang(): 'fa*' -> fa, everything else -> en.

When a user picks a language with /lang, their private chat gets its own list in that
language (chat scope overrides the language-based default). The admin chat also lists
/stats and /stats_export.
"""

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonCommands

from config import settings
from services.i18n import t

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "cmd_desc_start"),
    ("help", "cmd_desc_help"),
    ("lang", "cmd_desc_lang"),
    ("version", "cmd_desc_version"),
)
ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("stats", "cmd_desc_stats"),
    ("stats_export", "cmd_desc_stats_export"),
)


def build_commands(lang: str, include_admin: bool = False) -> list[BotCommand]:
    pairs = PUBLIC_COMMANDS + (ADMIN_COMMANDS if include_admin else ())
    return [BotCommand(command=name, description=t(key, lang=lang)) for name, key in pairs]


async def setup_bot_commands(bot: Bot) -> None:
    """Register the Menu Button command lists at startup. Never raises."""
    try:
        await bot.set_my_commands(build_commands("en"), scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            build_commands("fa"), scope=BotCommandScopeDefault(), language_code="fa"
        )
        await bot.set_my_commands(
            build_commands(settings.DEFAULT_LANG, include_admin=True),
            scope=BotCommandScopeChat(chat_id=settings.ADMIN_ID),
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Telegram menu commands registered (fa/en + admin)")
    except Exception as exc:
        logger.warning("Failed to register Telegram menu commands: %s", exc)


async def set_chat_commands_lang(bot: Bot, chat_id: int, lang: str) -> None:
    """Make one private chat's Menu Button follow the user's /lang choice. Never raises."""
    try:
        await bot.set_my_commands(
            build_commands(lang, include_admin=(chat_id == settings.ADMIN_ID)),
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except Exception as exc:
        logger.warning("Failed to set chat menu commands for lang=%s: %s", lang, exc)
