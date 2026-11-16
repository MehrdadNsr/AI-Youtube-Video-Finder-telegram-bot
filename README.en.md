# AI YouTube Video Finder Telegram Bot

[فارسی](README.md)

A Telegram bot that takes a short clip (for example a post forwarded from a Telegram channel), **finds the original YouTube video** and the **exact start timestamp** of the clip. Then, with one tap, you can get:

- Persian `.srt` subtitles, or bilingual Persian + English
- The video with embedded soft subtitles (`mov_text`, no re-encoding)
- A timestamped summary
- A simple HTML5 web player
- (Optional) direct streaming links for VLC / MX Player

The bot interface is bilingual (Persian and English). Users pick their language on the first `/start` and can change it any time with `/lang`; until then the bot uses `DEFAULT_LANG` (English). Subtitle and summary output is Persian for now.

Built with Python 3.11+ and `aiogram 3.x`.

## ⚠️ Disclaimer

This code is published for **educational and personal use only**. The author does not run any instance of this bot and is not responsible for how others use it. Anyone who runs it is **solely responsible** for complying with YouTube's Terms of Service, copyright law and the laws of their own country.

## How it works

1. Gemini extracts names and search queries from the post caption.
2. `yt-dlp` searches YouTube and scores candidates by title, channel and duration.
3. The first seconds of the clip are transcribed with Whisper (on Groq) and matched against candidate transcripts to find the exact start time.
4. Transcripts come from a 4-tier fallback chain: proxy, direct, Supadata, then Gemini video-URL transcription.
5. Subtitles and summaries are generated with Gemini models, falling back to OpenRouter if every Gemini model fails.

## Architecture

- **`config.py`**: validated environment configuration via `pydantic-settings`.
- **`services/llm_router.py`**: tiered LLM failover router (`fast` and `heavy` chains) with per-task timeouts, a circuit breaker, OpenRouter `json_schema` fallback and periodic model catalog validation.
- **`services/transcript_provider.py`**: 4-tier transcript engine (`youtube-transcript-api` via proxy, direct with a block cooldown, Supadata native captions, Gemini-URL multi-window transcription).
- **`services/audio_verifier.py`**: FFmpeg audio extraction + Whisper ASR + fuzzy matching to find the start timestamp.
- **`services/subtitle_service.py`**: translation, SRT, softsub muxing, summaries and the web player.
- **`services/ytdlp_options.py`**: shared `yt-dlp` options and startup checks for `deno` and `yt-dlp-ejs`.
- **`services/job_manager.py`**: FIFO concurrency queues, per-user rate limits, temp dir lifecycle, health watchdog and graceful shutdown.
- **`services/link_server.py`, `services/media_links.py`, `handlers/link_handler.py`**: optional signed, expiring direct-link server with HTTP Range support.
- **`services/analytics.py`, `middlewares/analytics_middleware.py`**: privacy-preserving SQLite (WAL) analytics with HMAC-SHA256 user hashes.
- **`services/i18n.py`**: all user-facing text in Persian and English, the `t()` lookup and per-request language via a contextvar.
- **`handlers/`**: Telegram commands, progress UI and inline buttons.

A per-file description (in Persian) is in [FILES.md](FILES.md).

## Requirements

| What | Where | Required? |
|---|---|---|
| A Linux server (VPS) with Python 3.11+ | anywhere | yes |
| FFmpeg | `apt install ffmpeg` | yes |
| Deno 2+ | [deno.com](https://deno.com) | yes, for YouTube downloads |
| Telegram bot token | [@BotFather](https://t.me/BotFather) | yes |
| Gemini API key | [Google AI Studio](https://aistudio.google.com/apikey) | yes (free tier is enough) |
| Groq API key | [console.groq.com](https://console.groq.com) | yes (free) |
| OpenRouter API key | [openrouter.ai](https://openrouter.ai) | no, fallback only |
| Supadata API key | [supadata.ai](https://supadata.ai) | no, transcript fallback only |

## Quick install (recommended)

On an Ubuntu or Debian server, run this. The script installs everything, asks for your keys and starts the bot:

```bash
git clone https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot.git && cd AI-Youtube-Video-Finder-telegram-bot && bash setup.sh
```

Full guide: [INSTALL.en.md](INSTALL.en.md). The script has not been tested on a real server yet; if it fails, please [open an issue](https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot/issues).

## Manual setup

### 1. System packages (Ubuntu / Debian)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg git unzip
```

```bash
curl -fsSL https://deno.land/install.sh | sh
```

```bash
sudo ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
```

### 2. Get the code

```bash
git clone https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot.git
```

```bash
cd AI-Youtube-Video-Finder-telegram-bot
```

### 3. Virtual environment and dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

### 4. Configuration

```bash
cp .env.example .env
```

Open `.env` and fill in at least:

- `TELEGRAM_BOT_TOKEN`
- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `ADMIN_ID`: your numeric Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot)); `/stats` only works for this ID
- `ANALYTICS_SALT`: a long random string, for example:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Everything else has sensible defaults.

### 5. Run

```bash
python main.py
```

Send `/start` to your bot in Telegram and forward it a video post.

### 6. Run as a service with systemd (optional)

Create `/etc/systemd/system/vsf-bot.service` (replace the user and paths):

```ini
[Unit]
Description=Video Source Finder Bot
After=network-online.target

[Service]
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/AI-Youtube-Video-Finder-telegram-bot
ExecStart=/home/YOUR_USER/AI-Youtube-Video-Finder-telegram-bot/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now vsf-bot
```

```bash
journalctl -u vsf-bot -f
```

## YouTube IP blocking

YouTube frequently blocks datacenter IPs (`RequestBlocked` in `youtube-transcript-api`, bot checks in `yt-dlp`). The bot has several fallbacks:

1. **Proxy**: set `YT_PROXY_HTTP` / `YT_PROXY_HTTPS`. A free, unofficial option is Cloudflare WARP through `wireproxy` on `127.0.0.1:8080`; it may stop working at any time. For high volume, use a rotating residential proxy.
2. **Supadata**: with `SUPADATA_API_KEY`, up to 90 free native transcripts per month.
3. **Gemini-URL transcription**: the Gemini free tier accepts about 8 hours of YouTube video per day. The bot's default cap is 7 hours (`GEMINI_URL_DAILY_LIMIT_SEC`). Suited to personal or small-scale use.

## Direct streaming links (optional, off by default)

Set `LINK_PUBLIC_BASE_URL` in `.env` (for example `http://YOUR_SERVER_IP:8081`) to add three buttons under every result:

- **`link_sub`**: MP4 with embedded Persian + English softsub tracks (max 720p)
- **`link_orig`**: the original video without subtitles (max 720p)
- **`link_audio`**: audio only (M4A)

Files are built on your server and served by a small built-in HTTP server with Range support. Links are HMAC-signed and expire after `LINK_TTL_SEC` (default 6 hours); anyone holding a link can download the file until then. Open `LINK_BIND_PORT` in your firewall and set a random `LINK_SIGNING_SECRET`. Leave `LINK_PUBLIC_BASE_URL` empty to keep the feature disabled.

## Bot commands

| Command | Who | What |
|---|---|---|
| `/start`, `/help` | everyone | welcome and usage |
| `/lang` | everyone | switch the bot language (Persian / English) |
| `/version` | everyone | bot version |
| `/stats`, `/stats_export` | `ADMIN_ID` only | usage analytics |

## Privacy

Analytics are stored in SQLite; user IDs are stored only as HMAC hashes. Message text, links and audio are never stored.

## Contributing

Issues and pull requests are welcome. Never put real keys or tokens in code or issues.

## License

[GNU AGPL v3](LICENSE). You may use and modify it freely, but if you run a modified version as a network service (for example a public bot), you must publish its source under the same license.
