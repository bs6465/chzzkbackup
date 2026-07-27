from fastapi.testclient import TestClient

from app.clipper import ClipWorker
from app.db import Database
from app.retention import RetentionService


def test_dashboard_separates_operational_jobs_from_recent_work(tmp_path, monkeypatch):
    from app import config, main

    final_root = tmp_path / "final"
    temp_dir = tmp_path / "temp"
    final_root.mkdir()
    temp_dir.mkdir()
    database = Database(tmp_path / "dashboard.sqlite3")
    database.upsert_channel("streamer-id", "Streamer", platform="chzzk")

    for index in range(6):
        session_id = database.create_session(
            "streamer-id",
            "Streamer",
            f"live-{index}",
            f"Recent title {index}",
            f"2026-07-2{index + 1}T10:00:00+09:00",
            temp_dir / f"recording-{index}.ts",
            temp_dir / f"chat-{index}.jsonl",
            temp_dir / f"chat-{index}.csv",
        )
        job_id = database.add_encode_job(
            session_id,
            temp_dir / f"source-{index}.ts",
            final_root / f"completed-{index}.mp4",
        )
        database.update_encode_job(job_id, "completed")
        database.update_session_status(
            session_id,
            "completed",
            final_path=final_root / f"completed-{index}.mp4",
        )

    active_session_id = database.create_session(
        "streamer-id",
        "Streamer",
        "live-active",
        "Queued title",
        "2026-07-27T10:00:00+09:00",
        temp_dir / "recording-active.ts",
        temp_dir / "chat-active.jsonl",
        temp_dir / "chat-active.csv",
    )
    database.add_encode_job(
        active_session_id,
        temp_dir / "queued-source.ts",
        final_root / "queued-output.mp4",
    )

    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "retention", RetentionService(database))
    monkeypatch.setattr(main, "clip_worker", ClipWorker(database))
    monkeypatch.setattr(config, "FINAL_ROOT", final_root)
    monkeypatch.setattr(config, "TEMP_DIR", temp_dir)
    client = TestClient(main.app)

    status = client.get("/partials/status")
    recent = client.get("/partials/recent-work")
    index = client.get("/")

    assert status.status_code == recent.status_code == index.status_code == 200
    assert "Queued title" in status.text
    assert "Recent title 5" not in status.text
    assert "completed-5.mp4" not in recent.text
    assert "/recordings/" not in recent.text
    assert "Recent title 5" in recent.text
    assert "Recent title 0" not in recent.text
    assert "Queued title" not in recent.text
    assert recent.text.count('class="row"') == 5
    assert '<details class="recent-work">' in index.text
