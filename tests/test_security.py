"""Offline security regression tests (no network, no Telegram)."""

import asyncio
import json
import os
import re

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456789:AAH_test_token_test_token_test_token")
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("GROQ_API_KEY", "test")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("ANALYTICS_SALT", "test_salt_test_salt_test_salt")

from handlers.video_handler import VideoActionCallback  # noqa: E402
from services.subtitle_service import (  # noqa: E402
    SubtitleSegment,
    SubtitleService,
    VideoMetadataInfo,
)


# ── Callback data is client-controlled ──────────────────────────

def test_callback_accepts_real_youtube_id() -> None:
    data = VideoActionCallback.unpack("vact:srt_fa:dQw4w9WgXcQ:42")
    assert data.video_id == "dQw4w9WgXcQ"
    assert data.start_sec == 42


@pytest.mark.parametrize(
    "packed",
    [
        "vact:mux_cc:../../../etc:0",       # path traversal into WORK_DIR
        "vact:web_play:abc'+alert(1)+'x:0",  # script injection into the player
        "vact:srt_fa:short:0",               # wrong length
        "vact:srt_fa:dQw4w9WgXcQ:-5",        # negative timestamp
    ],
)
def test_callback_rejects_forged_data(packed: str) -> None:
    with pytest.raises(ValueError):
        VideoActionCallback.unpack(packed)


# ── Web player must not execute caption text ───────────────────

def _build_player(text: str) -> str:
    meta = VideoMetadataInfo(
        video_id="dQw4w9WgXcQ",
        title="<b>title</b>",
        channel="chan",
        duration_seconds=60,
        thumbnail_url="",
    )
    seg = SubtitleSegment(index=1, start_sec=0.0, end_sec=2.0, en_text=text, fa_text=text)
    return SubtitleService.build_interactive_web_player_html(meta, [seg], start_seconds=0)


def test_player_caption_cannot_break_out_of_script() -> None:
    payload = "</script><script>alert(1)</script><img src=x onerror=alert(2)>"
    page = _build_player(payload)
    script = page.split("const SUBS = ", 1)[1].split(";\n", 1)[0]
    assert "<" not in script and ">" not in script
    assert json.loads(script)[0]["fa"] == payload  # data is preserved, only escaped
    assert "innerHTML" not in page
    assert page.count("</script>") == 2  # only the two real script tags


def test_player_title_is_escaped() -> None:
    page = _build_player("hi")
    assert "<b>title</b>" not in page
    assert "&lt;b&gt;title&lt;/b&gt;" in page


# ── Link server: no spoofable client IP ────────────────────────

def test_link_server_ignores_x_forwarded_for() -> None:
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "services", "link_server.py"),
        encoding="utf-8",
    ).read()
    handler = src.split("async def _handle_file_request", 1)[1].split("async def ", 1)[0]
    assert not re.search(r"headers\.get\(\s*[\"']X-Forwarded-For", handler)


def test_link_tokens_verify_and_reject_tampering(tmp_path) -> None:
    from config import settings
    from services.link_server import LinkServer

    settings_copy = settings.model_copy(update={"LINK_DIR": str(tmp_path)})
    server = LinkServer(settings_copy)
    token = server._sign_token("abcdef012345", 4102444800)
    assert server._verify_token(token) == ("abcdef012345", "ok")
    tampered = token.replace("abcdef012345", "abcdef012346")
    assert server._verify_token(tampered)[1] == "invalid"
    expired = server._sign_token("abcdef012345", 1000)
    assert server._verify_token(expired)[1] == "expired"
    asyncio.run(asyncio.sleep(0))
