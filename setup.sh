#!/usr/bin/env bash
# Video Source Finder Bot - interactive installer for Ubuntu / Debian.
#
# Usage (from the repository folder):
#   bash setup.sh                 full install: system packages, Python env, .env, systemd service
#   bash setup.sh --config-only   only (re)create .env and restart the service
#
# NOTE: this script has not been tested on a real fresh VPS yet.
# If something breaks, please open an issue with the full error output.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_DIR/.env"
ENV_EXAMPLE="$REPO_DIR/.env.example"
VENV_DIR="$REPO_DIR/.venv"
SERVICE_NAME="vsf-bot"
CONFIG_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --config-only) CONFIG_ONLY=1 ;;
    -h|--help)
      sed -n '2,10p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg (use --help)"
      exit 1
      ;;
  esac
done

# ── Output helpers ──────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
step() { echo; echo "${BOLD}==> $*${RESET}"; }
ok()   { echo "${GREEN}  ✓ $*${RESET}"; }
warn() { echo "${YELLOW}  ! $*${RESET}"; }
die()  { echo "${RED}  ✗ $*${RESET}" >&2; exit 1; }

# ask VAR "Question" "default"  -> visible input
ask() {
  local __var="$1" __q="$2" __def="${3:-}" __ans
  if [ -n "$__def" ]; then
    read -r -p "  $__q [$__def]: " __ans || true
    __ans="${__ans:-$__def}"
  else
    read -r -p "  $__q: " __ans || true
  fi
  printf -v "$__var" '%s' "$__ans"
}

# ask_secret VAR "Question" required|optional  -> hidden input
ask_secret() {
  local __var="$1" __q="$2" __mode="${3:-required}" __ans
  while true; do
    read -r -s -p "  $__q: " __ans || true
    echo
    if [ -n "$__ans" ] || [ "$__mode" = "optional" ]; then
      break
    fi
    warn "This value is required."
  done
  printf -v "$__var" '%s' "$__ans"
}

# ask_yes_no "Question" y|n  -> returns 0 for yes
ask_yes_no() {
  local __q="$1" __def="${2:-n}" __ans __hint="y/N"
  [ "$__def" = "y" ] && __hint="Y/n"
  read -r -p "  $__q [$__hint]: " __ans || true
  __ans="${__ans:-$__def}"
  case "$__ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

random_hex() { python3 -c "import secrets; print(secrets.token_hex(32))"; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
RUN_USER="$(id -un)"

echo "${BOLD}Video Source Finder Bot - setup${RESET}"
echo "Repository: $REPO_DIR"
[ -f "$ENV_EXAMPLE" ] || die "Run this script from the bot's repository folder (.env.example not found)."

# ── 1. System packages ─────────────────────────────────────────
if [ "$CONFIG_ONLY" -eq 0 ]; then
  step "Checking the operating system"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    ok "Detected: ${PRETTY_NAME:-unknown}"
  fi
  command -v apt-get >/dev/null 2>&1 || die "This installer supports Ubuntu / Debian (apt-get) only. See INSTALL.en.md for manual steps."
  if [ -n "$SUDO" ]; then
    command -v sudo >/dev/null 2>&1 || die "sudo is not installed. Run this script as root or install sudo."
  fi

  step "Installing system packages (python3, ffmpeg, git, curl, unzip)"
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg git curl unzip ca-certificates
  ok "System packages installed"

  step "Checking Python version (3.11+ required)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python $(python3 -V 2>&1) is too old. Use Ubuntu 24.04+ / Debian 12+, or install Python 3.11+ first."
  ok "$(python3 -V)"

  step "Installing Deno 2+ (needed by yt-dlp for YouTube downloads)"
  deno_ok=0
  if command -v deno >/dev/null 2>&1; then
    deno_major="$(deno --version 2>/dev/null | head -n1 | sed -E 's/^deno ([0-9]+).*/\1/')"
    if [ "${deno_major:-0}" -ge 2 ] 2>/dev/null; then deno_ok=1; fi
  fi
  if [ "$deno_ok" -eq 1 ]; then
    ok "Deno already installed: $(deno --version | head -n1)"
  else
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$HOME/.deno" sh -s -- -y
    $SUDO ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
    ok "Deno installed: $(/usr/local/bin/deno --version | head -n1)"
  fi

  step "Creating the Python environment and installing requirements"
  [ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"
  ok "Python requirements installed in .venv"
fi

# ── 2. Configuration (.env) ────────────────────────────────────
step "Configuring the bot (.env)"
if [ -f "$ENV_FILE" ]; then
  if ask_yes_no ".env already exists. Create a new one? (the old one is backed up)" n; then
    backup="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
    cp "$ENV_FILE" "$backup"
    ok "Backup saved: $backup"
  else
    ok "Keeping the existing .env"
    SKIP_ENV=1
  fi
fi

if [ "${SKIP_ENV:-0}" -ne 1 ]; then
  echo "  Keys are typed hidden. Nothing is sent anywhere except Telegram's own API to check the token."
  echo

  while true; do
    ask_secret BOT_TOKEN "Telegram bot token (from @BotFather)" required
    if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
      warn "That does not look like a bot token (format: 123456789:AA...)."
      continue
    fi
    bot_info="$(curl -sS --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || true)"
    if echo "$bot_info" | grep -q '"ok":true'; then
      bot_username="$(echo "$bot_info" | sed -nE 's/.*"username":"([^"]+)".*/\1/p')"
      ok "Token works: @${bot_username}"
      break
    fi
    if [ -z "$bot_info" ]; then
      warn "Could not reach Telegram to check the token (network?). Keeping it as entered."
      break
    fi
    warn "Telegram rejected this token. Please check it and try again."
  done

  ask_secret GEMINI_KEY "Gemini API key (https://aistudio.google.com/apikey)" required
  ask_secret GROQ_KEY "Groq API key (https://console.groq.com)" required

  while true; do
    ask ADMIN "Your numeric Telegram user ID (get it from @userinfobot)"
    [[ "$ADMIN" =~ ^[0-9]+$ ]] && break
    warn "The ID must be digits only."
  done

  while true; do
    ask DEF_LANG "Default bot language until each user picks one (en/fa)" "en"
    [[ "$DEF_LANG" =~ ^(en|fa)$ ]] && break
    warn "Type en or fa."
  done

  echo
  echo "  Optional (press Enter to skip):"
  ask_secret OPENROUTER_KEY "OpenRouter API key (backup when Gemini fails)" optional
  ask_secret SUPADATA_KEY "Supadata API key (backup for YouTube transcripts)" optional
  ask YT_PROXY "HTTP proxy for YouTube, e.g. http://127.0.0.1:8080 (only if YouTube blocks your server)" ""

  # Direct streaming links (served from THIS server)
  LINK_URL=""
  LINK_PORT="8081"
  echo
  echo "  Direct streaming links let users stream/download the video from THIS server"
  echo "  through a signed link that expires (default 6 hours). It uses your server's disk and bandwidth."
  if ask_yes_no "Enable direct streaming links?" n; then
    public_ip="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    ask LINK_PORT "Port for the link server" "8081"
    [[ "$LINK_PORT" =~ ^[0-9]+$ ]] || die "Port must be a number."
    default_url=""
    [ -n "$public_ip" ] && default_url="http://${public_ip}:${LINK_PORT}"
    ask LINK_URL "Public base URL of this server" "$default_url"
    [ -n "$LINK_URL" ] || die "A public URL is required to enable direct links."
    if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
      $SUDO ufw allow "${LINK_PORT}/tcp" >/dev/null
      ok "Firewall (ufw): port ${LINK_PORT}/tcp opened"
    else
      warn "Make sure port ${LINK_PORT}/tcp is open in your server/cloud firewall."
    fi
  fi

  # Write .env from .env.example, replacing only the keys we collected
  VSF_TELEGRAM_BOT_TOKEN="$BOT_TOKEN" \
  VSF_GEMINI_API_KEY="$GEMINI_KEY" \
  VSF_GROQ_API_KEY="$GROQ_KEY" \
  VSF_ADMIN_ID="$ADMIN" \
  VSF_DEFAULT_LANG="$DEF_LANG" \
  VSF_OPENROUTER_API_KEY="$OPENROUTER_KEY" \
  VSF_SUPADATA_API_KEY="$SUPADATA_KEY" \
  VSF_YT_PROXY_HTTP="$YT_PROXY" \
  VSF_YT_PROXY_HTTPS="$YT_PROXY" \
  VSF_LINK_PUBLIC_BASE_URL="$LINK_URL" \
  VSF_LINK_BIND_PORT="$LINK_PORT" \
  VSF_LINK_SIGNING_SECRET="$(random_hex)" \
  VSF_ANALYTICS_SALT="$(random_hex)" \
  python3 - "$ENV_EXAMPLE" "$ENV_FILE" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
values = {k[4:]: v for k, v in os.environ.items() if k.startswith("VSF_")}
out, seen = [], set()
for line in open(src, encoding="utf-8").read().splitlines():
    m = re.match(r"^([A-Z0-9_]+)=", line)
    if m and m.group(1) in values:
        key = m.group(1)
        out.append(f"{key}={values[key]}")
        seen.add(key)
    else:
        out.append(line)
for key in sorted(set(values) - seen):
    out.append(f"{key}={values[key]}")
old_umask = os.umask(0o077)
try:
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
finally:
    os.umask(old_umask)
PY
  chmod 600 "$ENV_FILE"
  ok ".env written (only readable by $RUN_USER)"
fi

# ── 3. Check the configuration loads ───────────────────────────
PYTHON_BIN="$VENV_DIR/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
step "Checking the configuration"
if (cd "$REPO_DIR" && "$PYTHON_BIN" -c "from config import settings" 2>/tmp/vsf_config_check.log); then
  ok "Configuration is valid"
else
  cat /tmp/vsf_config_check.log >&2
  die "The configuration has an error (see above). Fix .env or run: bash setup.sh --config-only"
fi

# ── 4. Run as a service ────────────────────────────────────────
step "Running the bot"
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  warn "systemd is not available. Start the bot manually:"
  echo "    cd \"$REPO_DIR\" && \"$PYTHON_BIN\" main.py"
  exit 0
fi

UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ "$CONFIG_ONLY" -eq 1 ] && [ -f "$UNIT_FILE" ]; then
  $SUDO systemctl restart "$SERVICE_NAME"
  ok "Service restarted with the new settings"
elif ask_yes_no "Install as a system service (starts on boot, restarts on crash)?" y; then
  $SUDO tee "$UNIT_FILE" >/dev/null <<UNIT
[Unit]
Description=Video Source Finder Bot
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${PYTHON_BIN} main.py
Restart=always
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now "$SERVICE_NAME"
  sleep 3
  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service '${SERVICE_NAME}' is running"
  else
    warn "The service did not start. Check the logs: journalctl -u ${SERVICE_NAME} -n 50"
  fi
  echo
  echo "  Logs:     journalctl -u ${SERVICE_NAME} -f"
  echo "  Restart:  sudo systemctl restart ${SERVICE_NAME}"
  echo "  Stop:     sudo systemctl stop ${SERVICE_NAME}"
else
  echo "  Start the bot manually:"
  echo "    cd \"$REPO_DIR\" && \"$PYTHON_BIN\" main.py"
fi

echo
echo "${GREEN}${BOLD}Done.${RESET} Open your bot in Telegram${bot_username:+ (@$bot_username)} and send /start."
echo "Problems? Open an issue: https://github.com/MehrdadNsr/video-source-finder-bot/issues"
