import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.media_library import MediaIndexer, load_chat_rows, normalize_chat_row, rename_media_item


@pytest.mark.asyncio
async def test_media_index_is_idempotent_and_tracks_missing(tmp_path, monkeypatch):
    from app import media_library

    root = tmp_path / "final"
    data = tmp_path / "data"
    channel = root / "Streamer"
    chat_dir = channel / "채팅"
    chat_dir.mkdir(parents=True)
    video = channel / "[260611 08-55-54] Streamer - Title.mp4"
    video.write_bytes(b"video")
    (chat_dir / "[260611 08-55-54] Streamer - Title.jsonl").write_text(
        json.dumps({"type": "chat", "timestamp": "now", "offset_seconds": 1, "nickname": "n", "content": "c"}) + "\n"
    )
    database = Database(tmp_path / "catalog.sqlite3")
    async def fake_probe(_path): return 123.0
    async def fake_thumbnail(_video, target, _duration):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"webp")
        return True
    monkeypatch.setattr(media_library, "db", database)
    monkeypatch.setattr(media_library.config, "FINAL_ROOT", root)
    monkeypatch.setattr(media_library.config, "APP_DATA_DIR", data)
    monkeypatch.setattr(media_library, "probe_duration", fake_probe)
    monkeypatch.setattr(media_library, "create_thumbnail", fake_thumbnail)
    indexer = MediaIndexer()

    assert await indexer.scan() == 1
    assert await indexer.scan() == 1
    assert database.media_summary() == {"total": 1, "available": 1, "unavailable": 0}

    video.unlink()
    assert await indexer.scan() == 0
    assert database.media_summary()["unavailable"] == 1

    video.write_bytes(b"video")
    assert await indexer.scan() == 1
    assert database.media_summary()["available"] == 1


def test_load_chat_prefers_jsonl_and_normalizes(tmp_path):
    path = tmp_path / "chat.jsonl"
    path.write_text(
        json.dumps({"type": "donation", "timestamp": "t", "offset_seconds": None, "nickname": "a", "content": "b", "raw": {"secret": 1}}) + "\n",
        encoding="utf-8",
    )
    rows = load_chat_rows({"chat_jsonl_path": str(path), "chat_csv_path": None})
    assert rows == [{
        "type": "donation", "timestamp": "t", "offset_seconds": None,
        "nickname": "a", "content": "b", "sync_state": "missing_video",
    }]
    assert normalize_chat_row({"offset_seconds": "2.5"})["sync_state"] == "synced"


def test_media_api_supports_range_and_rejects_missing(tmp_path, monkeypatch):
    from app import main, media_library

    root = tmp_path / "final"
    root.mkdir()
    video = root / "video.mp4"
    video.write_bytes(b"0123456789")
    database = Database(tmp_path / "api.sqlite3")
    media_id = database.upsert_media_item(
        video_path=video, channel_name="Streamer", title="Title",
        started_at="2026-06-11T08:55:54+09:00", size_bytes=10,
    )
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(media_library.config, "FINAL_ROOT", root)
    client = TestClient(main.app)

    response = client.get(f"/media/{media_id}/video", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["accept-ranges"] == "bytes"
    assert client.get("/media/999/video").status_code == 404


def test_database_backs_up_legacy_schema(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE recording_sessions (
        id INTEGER PRIMARY KEY, channel_id TEXT, channel_name TEXT, live_id TEXT,
        live_title TEXT, started_at TEXT, ended_at TEXT, status TEXT,
        temp_path TEXT, source_path TEXT, final_path TEXT, error TEXT
      );
    """)
    conn.commit()
    conn.close()

    Database(path)

    backups = list((tmp_path / "backups").glob("legacy-pre-v3-*.sqlite3"))
    assert len(backups) == 1


def test_media_listing_filters_and_paginates(tmp_path):
    database = Database(tmp_path / "list.sqlite3")
    for index in range(30):
        database.upsert_media_item(
            video_path=tmp_path / f"{index}.mp4",
            channel_name="Alice" if index % 2 else "Bob",
            title=f"Episode {index}",
            started_at=f"2026-06-{(index % 20) + 1:02d}T10:00:00+09:00",
            platform="chzzk", size_bytes=index,
        )
    rows, total = database.list_media(q="Episode", channel="Alice", page=1, page_size=5)
    assert total == 15
    assert len(rows) == 5
    assert all(row["channel_name"] == "Alice" for row in rows)
