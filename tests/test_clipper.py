import asyncio
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import config
from app.clipper import (
    ClipWorker,
    build_clip_command,
    build_clip_download_name,
    format_clip_timestamp,
)
from app.db import Database


def test_clip_command_uses_fast_stream_copy_and_requested_range(tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "clip.mp4.part"
    command = build_clip_command(source, output, 75, 135)

    assert command[command.index("-ss") + 1] == "75"
    assert command[command.index("-t") + 1] == "60"
    assert command[command.index("-c") + 1] == "copy"
    assert command[-2:] == ["mp4", str(output)]
    assert format_clip_timestamp(3661) == "01-01-01"
    assert (
        build_clip_download_name(source, 75, 135)
        == "source [00-01-15~00-02-15].mp4"
    )


@pytest.mark.asyncio
async def test_create_clip_job_validates_range_and_reuses_completed_result(
    tmp_path, monkeypatch
):
    root = tmp_path / "final"
    clip_dir = tmp_path / "clips"
    monkeypatch.setattr(config, "FINAL_ROOT", root)
    monkeypatch.setattr(config, "CLIP_TEMP_DIR", clip_dir)
    monkeypatch.setattr(config, "CLIP_MIN_FREE_BYTES", 0)
    database = Database(tmp_path / "catalog.sqlite3")
    video = root / "Streamer" / "source.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video" * 100)
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Source",
        started_at="2026-01-01T00:00:00+09:00",
        duration_seconds=100,
        size_bytes=video.stat().st_size,
    )
    worker = ClipWorker(database)

    with pytest.raises(ValueError):
        await worker.create_job(media_id, 20, 20)
    with pytest.raises(ValueError):
        await worker.create_job(media_id, 90, 101)

    job = await worker.create_job(media_id, 10, 20)
    output = Path(job["output_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"clip")
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    database.execute(
        "UPDATE clip_jobs SET status='completed', expires_at=? WHERE id=?",
        (expires.isoformat(), int(job["id"])),
    )

    reused = await worker.create_job(media_id, 10, 20)
    assert reused["id"] == job["id"]
    assert len(database.query_all("SELECT id FROM clip_jobs")) == 1


@pytest.mark.asyncio
async def test_clip_recovery_retries_only_once(tmp_path, monkeypatch):
    root = tmp_path / "final"
    clip_dir = tmp_path / "clips"
    monkeypatch.setattr(config, "FINAL_ROOT", root)
    monkeypatch.setattr(config, "CLIP_TEMP_DIR", clip_dir)
    monkeypatch.setattr(config, "CLIP_MIN_FREE_BYTES", 0)
    database = Database(tmp_path / "catalog.sqlite3")
    video = root / "Streamer" / "source.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Source",
        started_at="2026-01-01T00:00:00+09:00",
        duration_seconds=10,
        size_bytes=5,
    )
    worker = ClipWorker(database)
    job = await worker.create_job(media_id, 0, 5)
    database.execute(
        "UPDATE clip_jobs SET status='running' WHERE id=?", (int(job["id"]),)
    )

    first = worker.recover_interrupted_jobs()
    assert first["queued"] == 1
    recovered = worker.get_job(int(job["id"]))
    assert recovered["retry_count"] == 1

    database.execute(
        "UPDATE clip_jobs SET status='running' WHERE id=?", (int(job["id"]),)
    )
    second = worker.recover_interrupted_jobs()
    assert second["failed"] == 1
    assert worker.get_job(int(job["id"]))["status"] == "failed"


def test_clip_cleanup_removes_expired_files_and_old_records(tmp_path, monkeypatch):
    clip_dir = tmp_path / "clips"
    clip_dir.mkdir()
    monkeypatch.setattr(config, "CLIP_TEMP_DIR", clip_dir)
    database = Database(tmp_path / "catalog.sqlite3")
    now = datetime.now(timezone.utc)
    output = clip_dir / "clip-1.mp4"
    output.write_bytes(b"clip")
    cursor = database.execute(
        """
        INSERT INTO clip_jobs
          (source_title, start_seconds, end_seconds, download_name, output_path,
           status, created_at, finished_at, expires_at, updated_at)
        VALUES ('Source', 0, 5, 'clip.mp4', ?, 'completed', ?, ?, ?, ?)
        """,
        (
            str(output),
            (now - timedelta(days=8)).isoformat(),
            (now - timedelta(days=8)).isoformat(),
            (now - timedelta(hours=1)).isoformat(),
            (now - timedelta(days=8)).isoformat(),
        ),
    )
    worker = ClipWorker(database)

    result = worker.cleanup_expired(now)

    assert result["expired"] == 1
    assert result["records_deleted"] == 1
    assert not output.exists()
    assert worker.get_job(int(cursor.lastrowid)) is None


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
async def test_clip_worker_creates_playable_stream_copy_result(tmp_path, monkeypatch):
    root = tmp_path / "final"
    clip_dir = tmp_path / "clips"
    monkeypatch.setattr(config, "FINAL_ROOT", root)
    monkeypatch.setattr(config, "CLIP_TEMP_DIR", clip_dir)
    monkeypatch.setattr(config, "CLIP_MIN_FREE_BYTES", 0)
    video = root / "Streamer" / "source.mp4"
    video.parent.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000",
            "-t",
            "3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
    )
    database = Database(tmp_path / "catalog.sqlite3")
    media_id = database.upsert_media_item(
        video_path=video,
        channel_name="Streamer",
        title="Source",
        started_at="2026-01-01T00:00:00+09:00",
        duration_seconds=3,
        size_bytes=video.stat().st_size,
    )
    worker = ClipWorker(database)
    job = await worker.create_job(media_id, 0, 2)
    worker.start()
    try:
        for _ in range(100):
            current = worker.get_job(int(job["id"]))
            if current and current["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.05)
    finally:
        await worker.stop()

    completed = worker.get_job(int(job["id"]))
    assert completed["status"] == "completed", completed.get("error")
    assert completed["progress_percent"] == 100
    assert Path(completed["output_path"]).stat().st_size > 0
