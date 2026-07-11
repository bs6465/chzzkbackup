from datetime import datetime
import json
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from app.platforms import LiveInfo
from app.recorder import RecorderSupervisor, build_streamlink_command, recording_name


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


@pytest.mark.asyncio
async def test_channel_loop_rate_limits_network_errors(monkeypatch):
    supervisor = RecorderSupervisor()
    channel_id = "channel-1"
    warnings: list[str] = []
    exceptions: list[str] = []
    sleeps = 0

    class FakeDb:
        def get_channel(self, requested_channel_id):
            assert requested_channel_id == channel_id
            return {"id": channel_id, "active": True}

        def get_tokens(self):
            return {}

        def get_twitcasting_token(self):
            return ""

    async def fake_get_live_info(channel, tokens, twitcasting_token):
        raise aiohttp.ClientConnectionError("dns failed")

    async def fake_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            supervisor._network_error_log_at[channel_id] = -999
        if sleeps >= 4:
            supervisor._stop.set()

    monkeypatch.setattr("app.recorder.db", FakeDb())
    monkeypatch.setattr("app.recorder.get_live_info", fake_get_live_info)
    monkeypatch.setattr("app.recorder.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.recorder.config.POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("app.recorder.config.NETWORK_ERROR_LOG_INTERVAL_SECONDS", 300)
    monkeypatch.setattr("app.recorder.logger.warning", lambda message, *args: warnings.append(message % args))
    monkeypatch.setattr("app.recorder.logger.exception", lambda message, *args: exceptions.append(message % args))

    await supervisor.channel_loop(channel_id)

    assert warnings == [
        "Temporary network error while polling channel-1; retrying in 0s: dns failed",
        "Temporary network error while polling channel-1; retrying in 0s: dns failed",
    ]
    assert exceptions == []


def test_merge_segment_chats_folds_video_gaps_and_preserves_unsynced(tmp_path):
    first = tmp_path / "first.jsonl"
    missing = tmp_path / "missing.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps({"type":"chat","offset_seconds":2,"nickname":"a","content":"one"}) + "\n")
    missing.write_text(json.dumps({"type":"chat","offset_seconds":4,"nickname":"b","content":"gap"}) + "\n")
    second.write_text(json.dumps({"type":"chat","offset_seconds":3,"nickname":"c","content":"two"}) + "\n")
    segments = [
        {"chat_jsonl_path": str(first), "has_video": 1, "duration_seconds": 10},
        {"chat_jsonl_path": str(missing), "has_video": 0, "duration_seconds": None},
        {"chat_jsonl_path": str(second), "has_video": 1, "duration_seconds": 5},
    ]
    destination = tmp_path / "final.jsonl"

    RecorderSupervisor()._merge_segment_chats(segments, destination, tmp_path / "final.csv")

    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["offset_seconds"] for row in rows] == [2.0, None, 13.0]
    assert not first.exists()
    assert not missing.exists()
    assert not second.exists()
