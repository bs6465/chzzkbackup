from datetime import datetime
from zoneinfo import ZoneInfo

from app.platforms import LiveInfo
from app.recorder import build_streamlink_command, recording_name


def test_recording_name_uses_bracketed_short_date_format():
    started = datetime(2026, 6, 4, 0, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert (
        recording_name(started, "한도이 Doi", "제목", ".mp4")
        == "[260604 00-00-00] 한도이 Doi - 제목.mp4"
    )


def test_twitcasting_streamlink_command_uses_hls_fallbacks():
    live = LiveInfo(
        platform="twitcasting",
        live_id="123",
        title="Title",
        stream_url="https://twitcasting.tv/alice",
        stream_name="hls_high,hls_medium,hls_low,best",
    )

    command = build_streamlink_command("streamlink", "ffmpeg", live)

    assert "https://twitcasting.tv/alice" in command
    assert "hls_high,hls_medium,hls_low,best" in command
