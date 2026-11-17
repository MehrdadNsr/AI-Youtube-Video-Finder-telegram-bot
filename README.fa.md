<div dir="rtl">

# ربات تلگرام پیدا کردن منبع ویدیو در یوتیوب با هوش مصنوعی (AI YouTube Video Finder)

[English](README.md) | **فارسی**

📦 **نصب:** [INSTALL.fa.md](INSTALL.fa.md) (با یه دستور)

یه ربات تلگرام که یه کلیپ کوتاه (مثلاً یه پست فورواردی از یه کانال) رو می‌گیره، **ویدیوی اصلیش رو توی یوتیوب پیدا می‌کنه** و **ثانیهٔ دقیق شروع کلیپ** رو می‌گه. بعدش با یه کلیک می‌تونی این‌ها رو بگیری:

- زیرنویس فارسی (`.srt`) یا دوزبانهٔ فارسی و انگلیسی
- ویدیو با زیرنویس داخلی (softsub، بدون رندر دوباره)
- خلاصهٔ زمان‌دار ویدیو
- یه پخش‌کنندهٔ وب ساده
- (اختیاری) لینک مستقیم استریم برای VLC یا MX Player

منو و پیام‌های ربات دوزبانه‌ان (فارسی و انگلیسی). کاربر همون اول با `/start` زبانش رو انتخاب می‌کنه و بعداً با `/lang` عوضش می‌کنه. تا وقتی انتخاب نکرده، زبان پیش‌فرض (`DEFAULT_LANG`، انگلیسی) استفاده می‌شه. خروجی زیرنویس و خلاصه فعلاً فارسیه.

با پایتون ۳.۱۱ به بالا و `aiogram 3` نوشته شده.

## ⚠️ سلب مسئولیت

این کد فقط برای **آموزش و استفادهٔ شخصی** منتشر شده. نویسنده هیچ نسخه‌ای از این ربات رو اجرا نمی‌کنه و مسئول نحوهٔ استفادهٔ دیگران نیست. هر کسی که این ربات رو اجرا می‌کنه، **خودش مسئول** رعایت قوانین یوتیوب، قانون کپی‌رایت و قوانین کشور خودشه.

## چطوری کار می‌کنه؟

- **۱.** از کپشن پست، اسم‌ها و عبارت‌های جستجو با Gemini درمیاد.
- **۲.** با `yt-dlp` توی یوتیوب جستجو می‌شه و نتیجه‌ها امتیاز می‌گیرن.
- **۳.** صدای اول کلیپ با Whisper (روی Groq) به متن تبدیل می‌شه و با ترنسکریپت کاندیداها مقایسه می‌شه، تا ثانیهٔ دقیق شروع پیدا بشه.
- **۴.** ترنسکریپت از چند مسیر پشتیبان گرفته می‌شه (پروکسی، مستقیم، Supadata، و در آخر Gemini از روی لینک).
- **۵.** زیرنویس و خلاصه با مدل‌های Gemini ساخته می‌شن و اگه همه از دسترس خارج بودن، OpenRouter جایگزین می‌شه.

توضیح هر فایل توی [FILES.md](FILES.md) هست.

## چی لازم داری؟

| چی | از کجا | اجباری؟ |
|---|---|---|
| یه سرور لینوکسی (VPS) با پایتون ۳.۱۱+ | هر جا | آره |
| FFmpeg | `apt install ffmpeg` | آره |
| Deno نسخهٔ ۲ به بالا | [deno.com](https://deno.com) | برای دانلود از یوتیوب آره |
| توکن ربات تلگرام | [@BotFather](https://t.me/BotFather) | آره |
| کلید Gemini | [Google AI Studio](https://aistudio.google.com/apikey) | آره (پلن رایگان کافیه) |
| کلید Groq | [console.groq.com](https://console.groq.com) | آره (رایگان) |
| کلید OpenRouter | [openrouter.ai](https://openrouter.ai) | نه، فقط پشتیبان |
| کلید Supadata | [supadata.ai](https://supadata.ai) | نه، فقط پشتیبان ترنسکریپت |

## نصب سریع (پیشنهادی)

روی یه سرور اوبونتو یا دبیان این رو بزن. اسکریپت همه‌چی رو نصب می‌کنه، کلیدها رو ازت می‌پرسه و ربات رو راه میندازه:

```bash
git clone https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot.git && cd AI-Youtube-Video-Finder-telegram-bot && bash setup.sh
```

راهنمای کامل نصب: [INSTALL.fa.md](INSTALL.fa.md). اسکریپت هنوز روی سرور واقعی تست نشده؛ اگه خطا دیدی [Issue](https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot/issues) باز کن.

## راه‌اندازی دستی

### ۱. پیش‌نیازهای سیستم (اوبونتو یا دبیان)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg git unzip
```

```bash
curl -fsSL https://deno.land/install.sh | sh
```

```bash
sudo ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
```

### ۲. گرفتن کد

```bash
git clone https://github.com/MehrdadNsr/AI-Youtube-Video-Finder-telegram-bot.git
```

```bash
cd AI-Youtube-Video-Finder-telegram-bot
```

### ۳. محیط مجازی و نصب کتابخونه‌ها

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

### ۴. تنظیمات

```bash
cp .env.example .env
```

بعد `.env` رو باز کن و حداقل این‌ها رو پر کن:

- `TELEGRAM_BOT_TOKEN`: توکن از BotFather
- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `ADMIN_ID`: آیدی عددی تلگرام خودت (از [@userinfobot](https://t.me/userinfobot) بگیر)؛ دستور `/stats` فقط برای این آیدی کار می‌کنه
- `ANALYTICS_SALT`: یه رشتهٔ تصادفی طولانی. با این دستور بساز:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

بقیهٔ مقدارها پیش‌فرض معقول دارن و می‌تونی دست نزنی.

### ۵. اجرا

```bash
python main.py
```

حالا توی تلگرام به رباتت `/start` بده و یه پست ویدیویی براش فوروارد کن.

### ۶. اجرای دائمی با systemd (اختیاری)

یه فایل به اسم `/etc/systemd/system/vsf-bot.service` بساز. مسیرها و اسم کاربر رو با مال خودت عوض کن:

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

لاگ‌ها رو این‌جوری ببین:

```bash
journalctl -u vsf-bot -f
```

## مشکل بلاک شدن IP سرور توسط یوتیوب

یوتیوب خیلی وقت‌ها IP دیتاسنترها رو می‌بنده (خطای `RequestBlocked` یا bot-check). ربات چند تا راه پشتیبان داره:

- **۱.** **پروکسی**: `YT_PROXY_HTTP` و `YT_PROXY_HTTPS` رو توی `.env` بذار. یه راه رایگان و غیررسمی اینه که Cloudflare WARP رو با `wireproxy` روی `127.0.0.1:8080` بالا بیاری. ممکنه هر وقت از کار بیفته. برای ترافیک زیاد، پروکسی residential چرخشی بهتره.
- **۲.** **Supadata**: با `SUPADATA_API_KEY`، تا ۹۰ ترنسکریپت رایگان در ماه.
- **۳.** **Gemini از روی لینک**: پلن رایگان Gemini روزی حدود ۸ ساعت ویدیوی یوتیوب قبول می‌کنه. سقف پیش‌فرض ربات ۷ ساعته (`GEMINI_URL_DAILY_LIMIT_SEC`). برای استفادهٔ شخصی یا کم‌حجم مناسبه.

## لینک مستقیم استریم (اختیاری، پیش‌فرض خاموش)

اگه `LINK_PUBLIC_BASE_URL` رو توی `.env` بذاری (مثلاً `http://IP_SERVER:8081`)، زیر هر نتیجه سه دکمهٔ دیگه هم میاد:

- **`link_sub`**: فایل MP4 با زیرنویس فارسی و انگلیسی داخلی (حداکثر 720p)
- **`link_orig`**: ویدیوی اصلی بدون زیرنویس (حداکثر 720p)
- **`link_audio`**: فقط صدا (M4A)

فایل‌ها روی سرور خودت ساخته می‌شن و یه وب‌سرور کوچیک داخلی با پشتیبانی جلو و عقب بردن پخششون می‌کنه. لینک‌ها امضاشده‌ان و بعد از `LINK_TTL_SEC` (پیش‌فرض ۶ ساعت) منقضی می‌شن. هر کی لینک رو داشته باشه تا قبل از انقضا می‌تونه دانلود کنه. پورت `LINK_BIND_PORT` باید توی فایروال باز باشه. یه مقدار تصادفی هم برای `LINK_SIGNING_SECRET` بذار.

برای خاموش موندنش، `LINK_PUBLIC_BASE_URL` رو خالی بذار.

## دستورهای ربات

| دستور | کی می‌تونه | کار |
|---|---|---|
| `/start`، `/help` | همه | خوش‌آمد و راهنما |
| `/lang` | همه | تغییر زبان ربات (فارسی / English) |
| `/version` | همه | نسخهٔ ربات |
| `/stats`، `/stats_export` | فقط `ADMIN_ID` | آمار استفاده |

## حریم خصوصی

آمار توی SQLite ذخیره می‌شه و شناسهٔ کاربر فقط به‌صورت هش (HMAC) نگه داشته می‌شه. متن پیام، لینک یا صدا ذخیره نمی‌شه.

## مشارکت

Issue و Pull Request خوش‌اومدن. هیچ‌وقت کلید یا توکن واقعی رو توی کد یا Issue نذار.

## لایسنس

[GNU AGPL v3](LICENSE). خلاصه‌ش: آزادی استفاده و تغییرش بدی، ولی اگه نسخهٔ تغییریافته‌ش رو به‌شکل سرویس (مثلاً یه ربات عمومی) اجرا کنی، باید کدش رو با همین لایسنس منتشر کنی.

</div>
