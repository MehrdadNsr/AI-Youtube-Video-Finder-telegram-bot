# Installation Guide

**English** | [فارسی](INSTALL.fa.md)

> ⚠️ **Note:** the installer (`setup.sh`) has not been tested on a real server yet. If anything fails, please [open an issue](https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot/issues) with the full error output and we will fix it.

## Have these ready

| What | Where | Required? |
|---|---|---|
| An Ubuntu 24.04+ or Debian 12+ server | any VPS | yes |
| Telegram bot token | [@BotFather](https://t.me/BotFather), `/newbot` | yes |
| Gemini API key | [Google AI Studio](https://aistudio.google.com/apikey) (free) | yes |
| Groq API key | [console.groq.com](https://console.groq.com) (free) | yes |
| Your numeric Telegram user ID | [@userinfobot](https://t.me/userinfobot) | yes |
| OpenRouter API key | [openrouter.ai](https://openrouter.ai) | no |
| Supadata API key | [supadata.ai](https://supadata.ai) | no |

## One-command install (recommended)

On your server:

```bash
git clone https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot.git && cd AI-Youtube-Video-Finder-telegram-bot && bash setup.sh
```

The script:

1. Installs the requirements: Python, FFmpeg, Deno and the Python packages.
2. Asks for your keys one by one (typing is hidden, that is normal) and checks the bot token with Telegram.
3. Generates the internal secrets (`ANALYTICS_SALT`, `LINK_SIGNING_SECRET`) for you.
4. Asks whether you want direct streaming links; if yes, it detects your server IP and opens the port if `ufw` is active.
5. Writes `.env`, readable only by your user.
6. Installs the bot as a system service that starts on boot and restarts on crash.

When it finishes, open your bot in Telegram and send `/start`.

## After installing

| Task | Command |
|---|---|
| Follow the logs | `journalctl -u vsf-bot -f` |
| Restart | `sudo systemctl restart vsf-bot` |
| Stop | `sudo systemctl stop vsf-bot` |
| Change keys or settings | `bash setup.sh --config-only` |
| Update to the latest version | `git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart vsf-bot` |

Every other setting (models, limits, timeouts) lives in `.env` and is described in `.env.example`. Restart the bot after editing it.

## What are direct streaming links?

When enabled, each result gets three extra buttons: video with subtitles, original video, and audio only. Files are built and served from **your own server**, so they use your server's disk and bandwidth. Links are signed and expire after 6 hours. Off by default.

## Manual install

If the script does not work on your system, the manual steps are in the [README](README.md#manual-setup).

## Common problems

- **`Python ... is too old`**: the bot needs Python 3.11+. Use Ubuntu 24.04 / Debian 12 or newer.
- **YouTube blocks the server** (`RequestBlocked` or bot-check errors): see "YouTube IP blocking" in the [README](README.md). A proxy fixes it.
- **The bot does not answer**: check the logs first (`journalctl -u vsf-bot -n 50`). If they don't make sense, open an issue with the log. Never post your keys or token in an issue.
