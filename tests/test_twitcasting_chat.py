import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.twitcasting_chat import TwitCastingChatCapture


@pytest.mark.asyncio
async def test_twitcasting_chat_capture_writes_standard_csv_and_raw_jsonl(tmp_path):
    calls = []
    started_at = datetime(2026, 6, 3, 20, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    started_ts = int(started_at.timestamp())

    async def fake_comments(movie_id, access_token, *, limit, slice_id=None):
        calls.append(slice_id)
        return {
            "comments": [
                {
                    "id": "2",
                    "message": "second",
                    "from_user": {"name": "User B"},
                    "created": started_ts + 2,
                },
                {
                    "id": "1",
                    "message": "first",
                    "from_user": {"name": "User A"},
                    "created": started_ts + 1,
                },
                {
                    "id": "0",
                    "message": "old",
                    "from_user": {"name": "Old"},
                    "created": started_ts - 10,
                },
            ]
        }

    capture = TwitCastingChatCapture(
        "movie-1",
        "token",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
        started_at,
        comments_fetcher=fake_comments,
    )

    rows = await capture._fetch_new_comments()
    duplicate_rows = await capture._fetch_new_comments()

    assert calls == [None, "2"]
    assert duplicate_rows == []
    assert [row["content"] for row in rows] == ["first", "second"]
    assert rows[0]["timestamp"] == "2026-06-03T20:00:01+09:00"
    assert rows[0]["offset_seconds"] == 1.0
    assert rows[0]["raw"]["id"] == "1"


@pytest.mark.asyncio
async def test_twitcasting_chat_capture_run_outputs_files(tmp_path):
    started_at = datetime(2026, 6, 3, 20, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    started_ts = int(started_at.timestamp())

    async def fake_comments(movie_id, access_token, *, limit, slice_id=None):
        return {
            "comments": [
                {
                    "id": "1",
                    "message": "hello",
                    "from_user": {"name": "Alice"},
                    "created": started_ts + 1,
                }
            ]
        }

    capture = TwitCastingChatCapture(
        "movie-1",
        "token",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
        started_at,
        comments_fetcher=fake_comments,
    )

    stop_calls = 0

    def stop():
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls > 1

    await capture.run(stop)

    csv_lines = (tmp_path / "chat.csv").read_text(encoding="utf-8-sig").splitlines()
    jsonl_row = json.loads((tmp_path / "chat.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert csv_lines[0] == "type,timestamp,offset_seconds,nickname,content"
    assert csv_lines[1] == "chat,2026-06-03T20:00:01+09:00,1.0,Alice,hello"
    assert jsonl_row["raw"]["message"] == "hello"
