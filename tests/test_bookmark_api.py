from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.clipper import ClipWorker
from app.db import Database
from app.retention import RetentionService


def setup_app(tmp_path, monkeypatch):
    from app import main, media_library

    root = tmp_path / "final"
    root.mkdir()
    database = Database(tmp_path / "api.sqlite3")
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
        final_path=root / "video.mp4",
    )
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "retention", RetentionService(database))
    monkeypatch.setattr(main, "clip_worker", ClipWorker(database))
    monkeypatch.setattr(media_library.config, "FINAL_ROOT", root)
    return main, database, session_id, root, TestClient(main.app)


def test_live_bookmark_api_and_dashboard_controls(tmp_path, monkeypatch):
    _, database, session_id, _, client = setup_app(tmp_path, monkeypatch)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert f'data-session-id="{session_id}"' in dashboard.text
    assert "현재 시점 체크" in dashboard.text
    assert "구간 시작" in dashboard.text
    assert "채팅 기본" in dashboard.text
    assert "북마크 기본" in dashboard.text
    assert "북마크 메모 (선택)" in dashboard.text
    assert "hx-preserve" in dashboard.text
    assert '/static/bookmarks.js?v=0.4.6' in dashboard.text

    defaults = client.post(
        "/channels/streamer-id/sync-defaults",
        data={"chat_delay_seconds": "1.4", "bookmark_shift_seconds": "-2.6"},
        follow_redirects=False,
    )
    assert defaults.status_code == 303
    channel = database.get_channel("streamer-id")
    assert channel["default_chat_delay_seconds"] == 1.5
    assert channel["default_bookmark_shift_seconds"] == -2.5

    created = client.post(
        f"/recordings/{session_id}/bookmarks", json={"content": "important"}
    )
    assert created.status_code == 201
    bookmark = created.json()["bookmarks"][0]
    assert bookmark["content"] == "important"
    assert bookmark["resolved"] is False
    assert bookmark["kind"] == "point"

    opened_range = client.post(
        f"/recordings/{session_id}/bookmarks",
        json={"content": "range", "kind": "range"},
    )
    assert opened_range.status_code == 201
    live_range = next(
        item for item in opened_range.json()["bookmarks"] if item["content"] == "range"
    )
    assert live_range["complete"] is False
    ended_range = client.patch(
        f"/bookmarks/{live_range['id']}",
        json={"use_current_live_end_time": True},
    )
    assert ended_range.status_code == 200
    live_range = next(
        item for item in ended_range.json()["bookmarks"] if item["id"] == live_range["id"]
    )
    assert live_range["complete"] is True

    updated = client.patch(
        f"/bookmarks/{bookmark['id']}",
        json={"display_offset_seconds": 12, "content": "edited"},
    )
    assert updated.status_code == 200
    assert updated.json()["bookmarks"][0]["effective_offset_seconds"] == 12
    assert updated.json()["bookmarks"][0]["content"] == "edited"

    reset_to_live = client.patch(
        f"/bookmarks/{bookmark['id']}",
        json={"use_current_live_time": True, "content": "live now"},
    )
    assert reset_to_live.status_code == 200
    assert reset_to_live.json()["bookmarks"][0]["resolved"] is False

    too_long = client.post(
        f"/recordings/{session_id}/bookmarks", json={"content": "x" * 501}
    )
    assert too_long.status_code == 400

    database.update_session_status(session_id, "completed")
    ended = client.post(
        f"/recordings/{session_id}/bookmarks", json={"content": "late"}
    )
    assert ended.status_code == 409

    deleted = client.delete(f"/bookmarks/{bookmark['id']}")
    assert deleted.status_code == 200
    assert [item["id"] for item in deleted.json()["bookmarks"]] == [live_range["id"]]
    deleted_range = client.delete(f"/bookmarks/{live_range['id']}")
    assert deleted_range.json()["bookmarks"] == []


def test_media_bookmark_api_shift_and_player_controls(tmp_path, monkeypatch):
    _, database, session_id, root, client = setup_app(tmp_path, monkeypatch)
    database.add_recording_segment(
        session_id,
        1,
        None,
        None,
        None,
        datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc).isoformat(),
        duration_seconds=120,
        has_video=True,
    )
    live = client.post(
        f"/recordings/{session_id}/bookmarks", json={"content": "from live"}
    )
    assert live.status_code == 201
    database.update_session_status(session_id, "completed")
    video = root / "video.mp4"
    video.write_bytes(b"video")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Live title",
        started_at="2026-08-10T10:00:00+00:00",
        session_id=session_id,
        duration_seconds=120,
        size_bytes=5,
    )

    player = client.get(f"/library/{media_id}")
    assert player.status_code == 200
    assert 'id="bookmark-controls"' in player.text
    assert 'data-bookmark-scope="media"' in player.text
    assert "현재 위치 북마크" in player.text
    assert "구간 시작" in player.text
    assert 'data-seek-seconds="-10"' in player.text
    assert 'data-seek-seconds="10"' in player.text
    assert "북마크 전체 시간 보정" in player.text
    assert '/static/bookmarks.js?v=0.4.6' in player.text

    attached = client.get(f"/media/{media_id}/bookmarks")
    assert attached.status_code == 200
    assert attached.json()["bookmarks"][0]["content"] == "from live"
    assert attached.json()["bookmarks"][0]["media_id"] == media_id

    shifted = client.put(
        f"/media/{media_id}/bookmark-shift", json={"shift_seconds": 60.2}
    )
    assert shifted.status_code == 200
    assert shifted.json()["shift_seconds"] == 60

    chat_shifted = client.put(
        f"/media/{media_id}/chat-delay", json={"delay_seconds": -2.4}
    )
    assert chat_shifted.status_code == 200
    assert chat_shifted.json()["chat_delay_seconds"] == -2.5

    created = client.post(
        f"/media/{media_id}/bookmarks",
        json={"display_offset_seconds": 30, "content": "replay"},
    )
    assert created.status_code == 201
    replay = next(item for item in created.json()["bookmarks"] if item["content"] == "replay")
    assert replay["offset_seconds"] == -30
    assert replay["effective_offset_seconds"] == 30

    updated = client.patch(
        f"/bookmarks/{replay['id']}",
        json={"display_offset_seconds": 45, "content": "changed"},
    )
    changed = next(item for item in updated.json()["bookmarks"] if item["id"] == replay["id"])
    assert changed["offset_seconds"] == -15
    assert changed["effective_offset_seconds"] == 45
    assert changed["content"] == "changed"

    database.set_channel_sync_defaults("streamer-id", 1.5, -2.5)
    chat_reset = client.put(
        f"/media/{media_id}/chat-delay",
        json={"reset_to_channel_default": True},
    )
    assert chat_reset.json()["chat_delay_seconds"] == 1.5
    bookmark_reset = client.put(
        f"/media/{media_id}/bookmark-shift",
        json={"reset_to_channel_default": True},
    )
    assert bookmark_reset.json()["shift_seconds"] == -2.5

    deleted = client.delete(f"/bookmarks/{replay['id']}")
    assert deleted.status_code == 200
    assert all(item["id"] != replay["id"] for item in deleted.json()["bookmarks"])
