from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.db import Database
from app.retention import RetentionService


def add_media(
    database: Database,
    root: Path,
    *,
    name: str,
    started_at: str,
    size: int = 100,
) -> int:
    video = root / "Streamer" / f"{name}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"v" * size)
    return database.upsert_media_item(
        video_path=video,
        platform="chzzk",
        channel_name="Streamer",
        title=name,
        started_at=started_at,
        size_bytes=size,
    )


def test_schema_v4_backfills_unlimited_registered_and_discovered_policies(tmp_path):
    database = Database(tmp_path / "catalog.sqlite3")
    database.upsert_channel("stable-id", "Streamer", platform="chzzk")
    registered_id = database.upsert_media_item(
        video_path=tmp_path / "registered.mp4",
        channel_name="Streamer",
        title="Registered",
        started_at="2026-06-01T00:00:00+09:00",
    )
    discovered_id = database.upsert_media_item(
        video_path=tmp_path / "discovered.mp4",
        channel_name="Imported",
        title="Imported",
        started_at="2026-06-01T00:00:00+09:00",
    )

    assert database.query_one("PRAGMA user_version")["user_version"] == 4
    registered = database.get_media_item(registered_id)
    discovered = database.get_media_item(discovered_id)
    assert registered["retention_policy_key"] == "registered:chzzk:stable-id"
    assert discovered["retention_policy_key"].startswith("discovered:chzzk:")
    assert all(
        row["retention_days"] is None
        and row["max_items"] is None
        and row["max_bytes"] is None
        for row in database.query_all("SELECT * FROM retention_policies")
    )


def test_retention_combines_limits_and_excludes_protected_media(tmp_path):
    database = Database(tmp_path / "catalog.sqlite3")
    database.upsert_channel("stable-id", "Streamer", platform="chzzk")
    root = tmp_path / "final"
    ids = [
        add_media(
            database,
            root,
            name=f"item-{index}",
            started_at=f"2026-06-{day:02d}T00:00:00+09:00",
        )
        for index, day in enumerate((1, 20, 21, 22), start=1)
    ]
    forever_id = add_media(
        database,
        root,
        name="forever",
        started_at="2026-01-01T00:00:00+09:00",
        size=10_000,
    )
    scheduled_id = add_media(
        database,
        root,
        name="scheduled",
        started_at="2026-01-02T00:00:00+09:00",
        size=10_000,
    )
    service = RetentionService(database)
    key = "registered:chzzk:stable-id"
    service.set_policy(
        key,
        retention_days=25,
        max_items=2,
        max_bytes=150,
    )
    service.set_media_override([forever_id], "forever")
    service.set_media_override([scheduled_id], "scheduled", "2026-12-31")

    snapshot = service.evaluate(
        now=datetime(2026, 7, 1, tzinfo=timezone.utc)
    )

    assert {item["id"] for item in snapshot["candidates"]} == set(ids[:3])
    assert forever_id not in {item["id"] for item in snapshot["candidates"]}
    assert scheduled_id not in {item["id"] for item in snapshot["candidates"]}
    assert snapshot["items"][forever_id]["category"] == "forever"
    assert snapshot["items"][scheduled_id]["category"] == "scheduled"


def test_active_clip_defers_retention_candidate(tmp_path):
    database = Database(tmp_path / "catalog.sqlite3")
    root = tmp_path / "final"
    media_id = add_media(
        database,
        root,
        name="old",
        started_at="2025-01-01T00:00:00+09:00",
    )
    key = database.get_media_item(media_id)["retention_policy_key"]
    service = RetentionService(database)
    service.set_policy(key, retention_days=1, max_items=None, max_bytes=None)
    now = datetime.now(timezone.utc).isoformat()
    database.execute(
        """
        INSERT INTO clip_jobs
          (media_id, source_title, start_seconds, end_seconds, download_name,
           status, created_at, updated_at)
        VALUES (?, 'old', 0, 1, 'old.mp4', 'queued', ?, ?)
        """,
        (media_id, now, now),
    )

    snapshot = service.evaluate()

    assert snapshot["candidate_count"] == 0
    assert [item["id"] for item in snapshot["deferred"]] == [media_id]
    assert snapshot["items"][media_id]["deferred"] is True


def test_trash_restore_and_permanent_deletion_history(tmp_path, monkeypatch):
    root = tmp_path / "final"
    data = tmp_path / "data"
    monkeypatch.setattr(config, "FINAL_ROOT", root)
    monkeypatch.setattr(config, "APP_DATA_DIR", data)
    database = Database(tmp_path / "catalog.sqlite3")
    video = root / "Streamer" / "video.mp4"
    chat = root / "Streamer" / "채팅" / "video.jsonl"
    thumbnail = data / "thumbnail.webp"
    video.parent.mkdir(parents=True)
    chat.parent.mkdir(parents=True)
    thumbnail.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    chat.write_text("chat", encoding="utf-8")
    thumbnail.write_bytes(b"image")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Video",
        started_at="2026-01-01T00:00:00+09:00",
        chat_jsonl_path=chat,
        thumbnail_path=thumbnail,
        size_bytes=5,
    )
    service = RetentionService(database)

    assert service.trash_media(media_id, "test") is True
    assert not video.exists()
    assert database.get_media_item(media_id)["status"] == "trashed"
    video.write_bytes(b"collision")

    restored = service.restore_media(media_id)

    assert restored["status"] == "available"
    assert restored["retention_override"] == "forever"
    assert Path(restored["video_path"]).name == "video_1.mp4"
    assert Path(restored["video_path"]).read_bytes() == b"video"

    assert service.trash_media(media_id, "test again") is True
    assert service.purge_media(media_id) is True
    assert database.get_media_item(media_id) is None
    assert not thumbnail.exists()
    history = service.recent_deletions()
    assert history[0]["title"] == "Video"
    assert history[0]["reason"] == "test again"


def test_scheduled_expiry_is_protected_until_end_of_kst_day(tmp_path):
    database = Database(tmp_path / "catalog.sqlite3")
    root = tmp_path / "final"
    media_id = add_media(
        database,
        root,
        name="scheduled",
        started_at="2025-01-01T00:00:00+09:00",
    )
    service = RetentionService(database)
    service.set_media_override([media_id], "scheduled", "2026-07-27")

    before = service.evaluate(
        now=datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    )
    after = service.evaluate(
        now=datetime(2026, 7, 27, 16, tzinfo=timezone.utc)
    )

    assert before["candidate_count"] == 0
    assert after["candidate_count"] == 1
