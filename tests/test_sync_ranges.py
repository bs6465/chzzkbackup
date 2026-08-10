from datetime import datetime, timedelta, timezone

from app.bookmarks import BookmarkService
from app.db import Database


def make_media(database: Database, tmp_path, *, duration: float = 120) -> tuple[int, int]:
    database.upsert_channel("channel-id", "Streamer")
    database.set_channel_sync_defaults("channel-id", 1.5, -2.5)
    session_id = database.create_session(
        "channel-id",
        "Streamer",
        "live-id",
        "Title",
        "2026-08-10T10:00:00+00:00",
        tmp_path / "recording.ts.part",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
        final_path=tmp_path / "video.mp4",
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Title",
        started_at="2026-08-10T10:00:00+00:00",
        session_id=session_id,
        duration_seconds=duration,
        size_bytes=5,
    )
    return session_id, media_id


def test_channel_sync_defaults_snapshot_and_media_reset(tmp_path):
    database = Database(tmp_path / "sync.sqlite3")
    session_id, media_id = make_media(database, tmp_path)
    session = database.get_session(session_id)
    media = database.get_media_item(media_id)

    assert session["chat_delay_seconds"] == 1.5
    assert session["bookmark_shift_seconds"] == -2.5
    assert media["channel_id"] == "channel-id"
    assert media["chat_delay_seconds"] == 1.5
    assert media["bookmark_shift_seconds"] == -2.5

    service = BookmarkService(database)
    service.set_media_chat_delay(media_id, 99)
    service.set_media_shift(media_id, -99)
    assert service.media_sync(media_id)["chat_delay_seconds"] == 60
    assert service.media_collection(media_id)["shift_seconds"] == -60

    database.set_channel_sync_defaults("channel-id", 3.5, 4.5)
    assert service.media_sync(media_id)["chat_delay_seconds"] == 60
    assert service.media_collection(media_id)["shift_seconds"] == -60
    service.set_media_chat_delay(media_id, reset_to_channel_default=True)
    service.set_media_shift(media_id, reset_to_channel_default=True)
    assert service.media_sync(media_id)["chat_delay_seconds"] == 3.5
    assert service.media_collection(media_id)["shift_seconds"] == 4.5


def test_replay_range_bookmark_open_reverse_zero_and_conversion(tmp_path):
    database = Database(tmp_path / "ranges.sqlite3")
    _, media_id = make_media(database, tmp_path)
    service = BookmarkService(database)

    collection = service.create_media_bookmark(media_id, 40, "range", "range")
    bookmark = collection["bookmarks"][0]
    assert bookmark["kind"] == "range"
    assert bookmark["complete"] is False

    collection = service.update_bookmark(
        bookmark["id"],
        end_display_offset_seconds=20,
        end_offset_provided=True,
    )
    bookmark = collection["bookmarks"][0]
    assert bookmark["effective_offset_seconds"] == 20
    assert bookmark["effective_end_offset_seconds"] == 40

    collection = service.update_bookmark(
        bookmark["id"],
        display_offset_seconds=30,
        offset_provided=True,
        end_display_offset_seconds=30,
        end_offset_provided=True,
    )
    bookmark = collection["bookmarks"][0]
    assert bookmark["effective_offset_seconds"] == 30
    assert bookmark["effective_end_offset_seconds"] == 30

    collection = service.update_bookmark(
        bookmark["id"], kind="point", kind_provided=True
    )
    bookmark = collection["bookmarks"][0]
    assert bookmark["kind"] == "point"
    assert bookmark["end_offset_seconds"] is None
    collection = service.update_bookmark(
        bookmark["id"], kind="range", kind_provided=True
    )
    assert collection["bookmarks"][0]["complete"] is False


def test_multiple_live_ranges_map_both_boundaries_after_recording(tmp_path):
    database = Database(tmp_path / "live-ranges.sqlite3")
    database.upsert_channel("channel-id", "Streamer")
    start = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    session_id = database.create_session(
        "channel-id", "Streamer", "live-id", "Title", start.isoformat(),
        tmp_path / "recording.ts.part", tmp_path / "chat.jsonl", tmp_path / "chat.csv",
        final_path=tmp_path / "video.mp4",
    )
    database.add_recording_segment(
        session_id, 1, None, None, None, start.isoformat(),
        duration_seconds=60, has_video=True,
    )
    service = BookmarkService(database)
    first = service.create_live_bookmark(session_id, "first", "range")["bookmarks"][0]
    second = service.create_live_bookmark(session_id, "second", "range")["bookmarks"][1]
    database.execute(
        "UPDATE video_bookmarks SET marked_at=?, end_marked_at=? WHERE id=?",
        ((start + timedelta(seconds=10)).isoformat(), (start + timedelta(seconds=25)).isoformat(), first["id"]),
    )
    database.execute(
        "UPDATE video_bookmarks SET marked_at=? WHERE id=?",
        ((start + timedelta(seconds=30)).isoformat(), second["id"]),
    )
    database.update_session_status(session_id, "completed")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    media_id = database.upsert_media_item(
        video_path=video, channel_name="Streamer", title="Title",
        started_at=start.isoformat(), session_id=session_id,
        duration_seconds=60, size_bytes=5,
    )
    collection = service.media_collection(media_id)
    mapped = {item["content"]: item for item in collection["bookmarks"]}
    assert mapped["first"]["effective_offset_seconds"] == 10
    assert mapped["first"]["effective_end_offset_seconds"] == 25
    assert mapped["second"]["effective_offset_seconds"] == 30
    assert mapped["second"]["complete"] is False
