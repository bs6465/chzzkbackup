import pytest

from app.platforms import (
    PLATFORM_TWITCASTING,
    LiveInfo,
    get_live_info,
    normalize_channel_input,
)


def test_normalize_twitcasting_screen_id_variants():
    assert normalize_channel_input("twitcasting", "alice").display_id == "alice"
    assert normalize_channel_input("twitcasting", "@alice").display_id == "alice"
    normalized = normalize_channel_input("twitcasting", "https://twitcasting.tv/alice/movie/123")
    assert normalized.platform == PLATFORM_TWITCASTING
    assert normalized.internal_id == "twitcasting:alice"
    assert normalized.display_id == "alice"


def test_normalize_twitcasting_rejects_invalid_screen_id():
    with pytest.raises(ValueError):
        normalize_channel_input("twitcasting", "bad/user")


def test_normalize_chzzk_keeps_unique_id():
    normalized = normalize_channel_input("chzzk", "abc123")
    assert normalized.internal_id == "abc123"
    assert normalized.display_id == "abc123"


@pytest.mark.asyncio
async def test_twitcasting_current_live_maps_to_live_info(monkeypatch):
    async def fake_current_live(screen_id, access_token):
        assert screen_id == "alice"
        assert access_token == "token"
        return {
            "movie": {
                "id": "12345",
                "title": "Live / Title",
                "is_live": True,
            }
        }

    monkeypatch.setattr("app.platforms.get_current_live", fake_current_live)

    live = await get_live_info(
        {"platform": "twitcasting", "display_id": "alice", "id": "twitcasting:alice"},
        {},
        "token",
    )

    assert isinstance(live, LiveInfo)
    assert live.platform == "twitcasting"
    assert live.live_id == "12345"
    assert live.title == "Live Title"
    assert live.stream_url == "https://twitcasting.tv/alice"
    assert live.stream_name == "hls_high,hls_medium,hls_low,best"
