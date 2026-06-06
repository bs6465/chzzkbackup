from __future__ import annotations

from typing import Any

import aiohttp

from . import config
from .utils import sanitize_cookie_value


def cookie_header(tokens: dict[str, str]) -> str:
    nid_aut = sanitize_cookie_value(tokens.get("NID_AUT", ""))
    nid_ses = sanitize_cookie_value(tokens.get("NID_SES", ""))
    return f"NID_AUT={nid_aut}; NID_SES={nid_ses}"


def auth_headers(tokens: dict[str, str]) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Cookie": cookie_header(tokens),
        "Origin": "https://chzzk.naver.com",
        "Referer": "https://chzzk.naver.com/",
    }


def streamlink_header_args(tokens: dict[str, str]) -> list[str]:
    headers = {
        "Cookie": cookie_header(tokens),
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Origin": "https://chzzk.naver.com",
        "Referer": "https://chzzk.naver.com/",
    }
    args: list[str] = []
    for key, value in headers.items():
        args.extend(["--http-header", f"{key}={value}"])
    return args


async def fetch_json(url: str, tokens: dict[str, str]) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=auth_headers(tokens)) as response:
            response.raise_for_status()
            return await response.json()


async def get_live_detail(channel_id: str, tokens: dict[str, str]) -> dict[str, Any] | None:
    data = await fetch_json(config.LIVE_DETAIL_API.format(channel_id=channel_id), tokens)
    if data.get("code") != 200:
        return None
    content = data.get("content")
    return content if isinstance(content, dict) else None


async def get_channel_name(channel_id: str, tokens: dict[str, str]) -> str | None:
    try:
        data = await fetch_json(config.CHANNEL_API.format(channel_id=channel_id), tokens)
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, dict):
            name = content.get("channelName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass

    try:
        live = await get_live_detail(channel_id, tokens)
        channel = live.get("channel") if live else None
        if isinstance(channel, dict):
            name = channel.get("channelName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass
    return None


async def test_tokens(tokens: dict[str, str], channel_id: str | None = None) -> tuple[bool, str]:
    target = channel_id or "00000000000000000000000000000000"
    try:
        await fetch_json(config.CHANNEL_API.format(channel_id=target), tokens)
    except aiohttp.ClientResponseError as exc:
        if exc.status in {401, 403}:
            return False, f"Token rejected by Chzzk API: HTTP {exc.status}"
        if channel_id is None and exc.status == 404:
            return True, "Token headers were accepted; test channel does not exist."
        return False, f"Chzzk API returned HTTP {exc.status}"
    except Exception as exc:
        return False, f"Token test failed: {exc}"
    return True, "Token test request completed."
