from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.clipper import ClipWorker
from app.db import Database
from app.retention import RetentionService


def test_retention_and_clip_forms_render_and_update(tmp_path, monkeypatch):
    from app import main, media_library

    root = tmp_path / "final"
    clip_dir = tmp_path / "clips"
    root.mkdir()
    video = root / "Streamer" / "video.mp4"
    video.parent.mkdir()
    video.write_bytes(b"0123456789")
    database = Database(tmp_path / "api.sqlite3")
    database.upsert_channel("streamer-id", "Streamer", platform="chzzk")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title='Video & "Capture"',
        started_at="2026-06-11T08:55:54+09:00",
        duration_seconds=10,
        size_bytes=10,
    )
    service = RetentionService(database)
    worker = ClipWorker(database)
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "retention", service)
    monkeypatch.setattr(main, "clip_worker", worker)
    monkeypatch.setattr(media_library.config, "FINAL_ROOT", root)
    monkeypatch.setattr(config, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(config, "CLIP_TEMP_DIR", clip_dir)
    monkeypatch.setattr(config, "CLIP_MIN_FREE_BYTES", 0)
    client = TestClient(main.app)

    index = client.get("/")
    library = client.get("/library")
    player = client.get(f"/library/{media_id}")
    assert index.status_code == library.status_code == player.status_code == 200
    assert "저장 정책 · 휴지통" in index.text
    assert "선택 영상 저장 정책" in library.text
    assert "구간 다운로드" in player.text
    assert 'id="capture-frame"' in player.text
    assert 'data-media-title="Video &amp; &#34;Capture&#34;" disabled' in player.text
    assert "스크린샷 저장 (S)" in player.text
    assert 'id="capture-status"' in player.text
    assert 'aria-live="polite"' in player.text
    assert '/static/styles.css?v=0.4.6' in player.text
    assert '/static/player.js?v=0.4.6' in player.text
    assert 'id="chat-delay-earlier"' in player.text
    assert 'id="chat-delay-later"' in player.text
    assert 'id="chat-delay-reset"' in player.text
    assert 'id="chat-delay-value"' in player.text

    key = database.get_media_item(media_id)["retention_policy_key"]
    preview = client.post(
        "/retention/policies/preview",
        data={
            "policy_key": key,
            "retention_days": "1",
            "max_items": "",
            "max_gib": "",
        },
    )
    assert preview.status_code == 200
    assert "예상 정리" in preview.text

    saved = client.post(
        "/retention/policies",
        data={
            "policy_key": key,
            "retention_days": "90",
            "max_items": "10",
            "max_gib": "2.5",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    policy = database.query_one(
        "SELECT * FROM retention_policies WHERE policy_key=?", (key,)
    )
    assert policy["retention_days"] == 90
    assert policy["max_items"] == 10
    assert policy["max_bytes"] == int(2.5 * 1024**3)

    override = client.post(
        f"/media/{media_id}/retention",
        data={"retention_override": "scheduled", "expires_on": "2026-12-31"},
        follow_redirects=False,
    )
    assert override.status_code == 303
    assert database.get_media_item(media_id)["retention_override"] == "scheduled"

    created = client.post(
        f"/media/{media_id}/clips",
        data={"start_time": "00:00:01", "end_time": "00:00:05"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    job = worker.jobs(media_id=media_id)[0]
    assert job["status"] == "queued"

    output = Path(job["output_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"clip-data")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    database.execute(
        "UPDATE clip_jobs SET status='completed', expires_at=? WHERE id=?",
        (expires.isoformat(), int(job["id"])),
    )
    download = client.get(
        f"/clips/{job['id']}/download",
        headers={"Range": "bytes=0-3"},
    )
    assert download.status_code == 206
    assert download.content == b"clip"


def test_clip_form_rejects_invalid_time_format(tmp_path, monkeypatch):
    from app import main, media_library

    root = tmp_path / "final"
    root.mkdir()
    video = root / "video.mp4"
    video.write_bytes(b"video")
    database = Database(tmp_path / "api.sqlite3")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Video",
        started_at="2026-06-11T08:55:54+09:00",
        duration_seconds=10,
        size_bytes=5,
    )
    worker = ClipWorker(database)
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "clip_worker", worker)
    monkeypatch.setattr(media_library.config, "FINAL_ROOT", root)
    client = TestClient(main.app)

    response = client.post(
        f"/media/{media_id}/clips",
        data={"start_time": "1:2", "end_time": "00:00:05"},
    )
    assert response.status_code == 400
