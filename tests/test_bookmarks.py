from datetime import datetime, timedelta, timezone

import pytest

from app.bookmarks import (
    BookmarkService,
    BookmarkValidationError,
    effective_bookmark_offset,
    normalize_bookmark_shift,
)
from app.db import Database


def make_session(database: Database, tmp_path, *, status: str = "recording") -> int:
    database.upsert_channel("streamer-id", "Streamer")
    session_id = database.create_session(
        "streamer-id",
        "Streamer",
        "live-1",
        "Live title",
        "2026-08-10T10:00:00+00:00",
        tmp_path / "recording.ts.part",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
        final_path=tmp_path / "video.mp4",
    )
    if status != "recording":
        database.update_session_status(session_id, status)
    return session_id


def add_segment(
    database: Database,
    session_id: int,
    sequence: int,
    started_at: datetime,
    duration: float,
    *,
    has_video: bool = True,
) -> None:
    database.add_recording_segment(
        session_id,
        sequence,
        None,
        None,
        None,
        started_at.isoformat(),
        duration_seconds=duration,
        has_video=has_video,
    )


def test_bookmark_schema_and_shift_normalization(tmp_path):
    database = Database(tmp_path / "bookmarks.sqlite3")

    columns = {
        row["name"]
        for row in database.query_all("PRAGMA table_info(video_bookmarks)")
    }
    media_columns = {
        row["name"] for row in database.query_all("PRAGMA table_info(media_items)")
    }
    session_columns = {
        row["name"]
        for row in database.query_all("PRAGMA table_info(recording_sessions)")
    }

    assert {"session_id", "media_id", "marked_at", "offset_seconds", "content"} <= columns
    assert "bookmark_shift_seconds" in media_columns
    assert "current_segment_started_at" in session_columns
    assert normalize_bookmark_shift(1.24) == 1.0
    assert normalize_bookmark_shift(1.26) == 1.5
    assert normalize_bookmark_shift(1.25) == 1.5
    assert normalize_bookmark_shift(-99) == -60
    assert normalize_bookmark_shift(99) == 60
    assert effective_bookmark_offset(3, -5, 20) == 0
    assert effective_bookmark_offset(18, 5, 20) == 20


def test_live_bookmarks_map_to_concatenated_segments_and_attach_to_media(tmp_path):
    database = Database(tmp_path / "bookmarks.sqlite3")
    session_id = make_session(database, tmp_path)
    start = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
    add_segment(database, session_id, 1, start, 10)
    add_segment(database, session_id, 2, start + timedelta(seconds=30), 20)
    service = BookmarkService(database)

    first = database.execute(
        """
        INSERT INTO video_bookmarks
          (session_id, marked_at, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_id,
            (start + timedelta(seconds=5)).isoformat(),
            "first",
            start.isoformat(),
            start.isoformat(),
        ),
    ).lastrowid
    gap = database.execute(
        """
        INSERT INTO video_bookmarks
          (session_id, marked_at, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_id,
            (start + timedelta(seconds=24)).isoformat(),
            "gap",
            start.isoformat(),
            start.isoformat(),
        ),
    ).lastrowid
    second = database.execute(
        """
        INSERT INTO video_bookmarks
          (session_id, marked_at, content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_id,
            (start + timedelta(seconds=37)).isoformat(),
            "second",
            start.isoformat(),
            start.isoformat(),
        ),
    ).lastrowid

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Live title",
        started_at=start.isoformat(),
        session_id=session_id,
        duration_seconds=30,
        size_bytes=5,
    )
    service.attach_session_bookmarks(media_id)

    rows = {
        row["id"]: row
        for row in database.query_all(
            "SELECT * FROM video_bookmarks WHERE media_id=?", (media_id,)
        )
    }
    assert rows[first]["offset_seconds"] == 5
    assert rows[gap]["offset_seconds"] == 10
    assert rows[second]["offset_seconds"] == 17


def test_media_bookmark_shift_inverse_edit_and_delete(tmp_path):
    database = Database(tmp_path / "bookmarks.sqlite3")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Video",
        started_at="2026-08-10T10:00:00+00:00",
        duration_seconds=120,
        size_bytes=5,
    )
    service = BookmarkService(database)

    collection = service.set_media_shift(media_id, 2.5)
    assert collection["shift_seconds"] == 2.5
    collection = service.create_media_bookmark(media_id, 30, "interesting")
    bookmark = collection["bookmarks"][0]
    assert bookmark["offset_seconds"] == 27.5
    assert bookmark["effective_offset_seconds"] == 30

    collection = service.update_bookmark(
        bookmark["id"],
        content="updated",
        content_provided=True,
        display_offset_seconds=45,
        offset_provided=True,
    )
    bookmark = collection["bookmarks"][0]
    assert bookmark["content"] == "updated"
    assert bookmark["offset_seconds"] == 42.5
    assert bookmark["effective_offset_seconds"] == 45

    collection = service.delete_bookmark(bookmark["id"])
    assert collection["bookmarks"] == []


def test_live_bookmark_content_and_manual_time_validation(tmp_path):
    database = Database(tmp_path / "bookmarks.sqlite3")
    session_id = make_session(database, tmp_path)
    service = BookmarkService(database)

    collection = service.create_live_bookmark(session_id)
    bookmark_id = collection["bookmarks"][0]["id"]
    collection = service.update_bookmark(
        bookmark_id,
        content="manual",
        content_provided=True,
        display_offset_seconds=12,
        offset_provided=True,
    )
    assert collection["bookmarks"][0]["effective_offset_seconds"] == 12
    assert collection["bookmarks"][0]["resolved"] is True

    with pytest.raises(BookmarkValidationError):
        service.create_live_bookmark(session_id, "x" * 501)
    with pytest.raises(BookmarkValidationError):
        service.update_bookmark(
            bookmark_id,
            content="two\nlines",
            content_provided=True,
        )
