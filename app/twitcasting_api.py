from __future__ import annotations

from typing import Any

import aiohttp

from .utils import sanitize_cookie_value

API_ROOT = "https://apiv2.twitcasting.tv"


def normalize_access_token(value: Any) -> str:
    return sanitize_cookie_value(value)


def auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Api-Version": "2.0",
        "Authorization": f"Bearer {normalize_access_token(access_token)}",
    }


async def fetch_json(path: str, access_token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=20)
    url = f"{API_ROOT}{path}"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, headers=auth_headers(access_token)) as response:
            response.raise_for_status()
            return await response.json()


async def get_user_name(screen_id: str, access_token: str) -> str | None:
    if not access_token:
        return None
    try:
        data = await fetch_json(f"/users/{screen_id}", access_token)
    except Exception:
        return None
    user = data.get("user") if isinstance(data, dict) else None
    if isinstance(user, dict):
        name = user.get("name") or user.get("screen_id")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


async def get_current_live(screen_id: str, access_token: str) -> dict[str, Any] | None:
    if not access_token:
        return None
    try:
        data = await fetch_json(f"/users/{screen_id}/current_live", access_token)
    except aiohttp.ClientResponseError as exc:
        if exc.status == 404:
            return None
        raise
    movie = data.get("movie") if isinstance(data, dict) else None
    if not isinstance(movie, dict) or not movie.get("is_live"):
        return None
    return data


async def get_comments(
    movie_id: str,
    access_token: str,
    *,
    limit: int = 50,
    slice_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if slice_id:
        params["slice_id"] = slice_id
    return await fetch_json(f"/movies/{movie_id}/comments", access_token, params=params)


async def test_token(access_token: str, screen_id: str | None = None) -> tuple[bool, str]:
    if not access_token:
        return False, "TwitCasting access token is not set."
    target = screen_id or "twitcasting_jp"
    try:
        await fetch_json(f"/users/{target}", access_token)
    except aiohttp.ClientResponseError as exc:
        if exc.status in {401, 403}:
            return False, f"TwitCasting token rejected: HTTP {exc.status}"
        return False, f"TwitCasting API returned HTTP {exc.status}"
    except Exception as exc:
        return False, f"TwitCasting token test failed: {exc}"
    return True, "TwitCasting token test request completed."
