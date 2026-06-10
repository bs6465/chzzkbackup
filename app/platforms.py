from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .chat_capture import ChatCapture
from .chzzk_api import get_channel_name as get_chzzk_channel_name
from .chzzk_api import get_live_detail as get_chzzk_live_detail
from .chzzk_api import streamlink_header_args
from .twitcasting_api import get_current_live, get_user_name
from .twitcasting_chat import TwitCastingChatCapture
from .utils import SAFE_CHANNEL_ID, sanitize_name

PLATFORM_CHZZK = "chzzk"
PLATFORM_TWITCASTING = "twitcasting"
PLATFORM_LABELS = {
    PLATFORM_CHZZK: "치지직",
    PLATFORM_TWITCASTING: "트윗캐스트",
}
TWITCASTING_SCREEN_ID = re.compile(r"^[A-Za-z0-9_]{1,64}$")
TWITCASTING_URL = re.compile(r"^https?://(?:[^/]+\.)?twitcasting\.tv/(?P<screen_id>[A-Za-z0-9_]{1,64})(?:[/?#].*)?$")


@dataclass(frozen=True)
class NormalizedChannel:
    platform: str
    internal_id: str
    display_id: str


@dataclass(frozen=True)
class LiveInfo:
    platform: str
    live_id: str
    title: str
    stream_url: str
    stream_name: str
    streamlink_args: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def supported_platforms() -> list[dict[str, str]]:
    return [
        {"id": PLATFORM_CHZZK, "label": PLATFORM_LABELS[PLATFORM_CHZZK]},
        {"id": PLATFORM_TWITCASTING, "label": PLATFORM_LABELS[PLATFORM_TWITCASTING]},
    ]


def platform_label(platform: str | None) -> str:
    return PLATFORM_LABELS.get(platform or PLATFORM_CHZZK, platform or PLATFORM_CHZZK)


def normalize_channel_input(platform: str, value: str) -> NormalizedChannel:
    platform = platform if platform in PLATFORM_LABELS else PLATFORM_CHZZK
    text = value.strip()
    if platform == PLATFORM_CHZZK:
        if not SAFE_CHANNEL_ID.fullmatch(text):
            raise ValueError("Invalid Chzzk unique ID")
        return NormalizedChannel(platform, text, text)

    screen_id = _normalize_twitcasting_screen_id(text)
    return NormalizedChannel(platform, f"{PLATFORM_TWITCASTING}:{screen_id}", screen_id)


def _normalize_twitcasting_screen_id(value: str) -> str:
    text = value.strip()
    match = TWITCASTING_URL.match(text)
    if match:
        text = match["screen_id"]
    if text.startswith("@"):
        text = text[1:]
    if not TWITCASTING_SCREEN_ID.fullmatch(text):
        raise ValueError("Invalid TwitCasting screen ID")
    return text


async def get_channel_name(channel: NormalizedChannel, tokens: dict[str, str], twitcasting_token: str) -> str | None:
    if channel.platform == PLATFORM_TWITCASTING:
        return await get_user_name(channel.display_id, twitcasting_token)
    return await get_chzzk_channel_name(channel.display_id, tokens)


async def get_live_info(channel: dict[str, Any], tokens: dict[str, str], twitcasting_token: str) -> LiveInfo | None:
    platform = str(channel.get("platform") or PLATFORM_CHZZK)
    display_id = str(channel.get("display_id") or channel["id"])
    if platform == PLATFORM_TWITCASTING:
        return await _get_twitcasting_live_info(display_id, twitcasting_token)
    return await _get_chzzk_live_info(display_id, tokens)


def create_chat_capture(
    channel: dict[str, Any],
    live: LiveInfo,
    tokens: dict[str, str],
    twitcasting_token: str,
    jsonl_path: Path,
    csv_path: Path,
    recording_started_at: datetime,
) -> Any | None:
    if live.platform == PLATFORM_TWITCASTING:
        if not twitcasting_token:
            return None
        return TwitCastingChatCapture(live.live_id, twitcasting_token, jsonl_path, csv_path, recording_started_at)
    channel_id = str(channel.get("display_id") or channel["id"])
    return ChatCapture(channel_id, tokens, jsonl_path, csv_path, recording_started_at)


async def _get_chzzk_live_info(channel_id: str, tokens: dict[str, str]) -> LiveInfo | None:
    live = await get_chzzk_live_detail(channel_id, tokens)
    if not live or live.get("status") != "OPEN":
        return None
    return LiveInfo(
        platform=PLATFORM_CHZZK,
        live_id=str(live.get("liveId") or ""),
        title=str(live.get("liveTitle") or "untitled"),
        stream_url=f"https://chzzk.naver.com/live/{channel_id}",
        stream_name="best",
        streamlink_args=[
            "--plugin-dirs",
            str(config.STREAMLINK_PLUGIN_DIR),
            *streamlink_header_args(tokens),
        ],
        raw=live,
    )


async def _get_twitcasting_live_info(screen_id: str, access_token: str) -> LiveInfo | None:
    live = await get_current_live(screen_id, access_token)
    if not live:
        return None
    movie = live.get("movie") if isinstance(live, dict) else None
    if not isinstance(movie, dict):
        return None
    title = sanitize_name(movie.get("title"), f"live-{movie.get('id') or 'untitled'}")
    return LiveInfo(
        platform=PLATFORM_TWITCASTING,
        live_id=str(movie.get("id") or ""),
        title=title,
        stream_url=f"https://twitcasting.tv/{screen_id}",
        stream_name="hls_high,hls_medium,hls_low,best",
        raw=live,
    )
