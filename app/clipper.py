from __future__ import annotations

import asyncio
import os
import shutil
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config
from .db import Database, db
from .encoder import parse_ffmpeg_seconds, probe_duration_seconds, terminate_process
from .logger import logger
from .media_library import within_final_root
from .utils import shorten_filename, utc_now_iso


class InsufficientClipStorageError(ValueError):
    pass


def format_clip_timestamp(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}"


def build_clip_download_name(source_path: Path, start_seconds: int, end_seconds: int) -> str:
    return shorten_filename(
        f"{source_path.stem} "
        f"[{format_clip_timestamp(start_seconds)}~{format_clip_timestamp(end_seconds)}].mp4"
    )


def build_clip_command(
    source_path: Path,
    output_path: Path,
    start_seconds: int,
    end_seconds: int,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-nostats",
        "-progress",
        "pipe:2",
        "-ss",
        str(start_seconds),
        "-i",
        str(source_path),
        "-t",
        str(end_seconds - start_seconds),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]


async def read_clip_progress(
    stream: asyncio.StreamReader,
    database: Database,
    job_id: int,
    requested_duration: int,
) -> str:
    values: dict[str, str] = {}
    stderr_tail: deque[str] = deque(maxlen=80)
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode(errors="replace").strip()
        if not text:
            continue
        if "=" not in text:
            stderr_tail.append(text)
            continue
        key, value = text.split("=", 1)
        values[key] = value
        if key != "progress":
            continue
        encoded = (
            parse_ffmpeg_seconds(values.get("out_time_us", ""))
            or parse_ffmpeg_seconds(values.get("out_time_ms", ""))
            or parse_ffmpeg_seconds(values.get("out_time", ""))
            or 0.0
        )
        progress = min(99.9, max(0.0, encoded / requested_duration * 100))
        if value == "end":
            progress = 100.0
        database.execute(
            """
            UPDATE clip_jobs SET progress_percent=?, updated_at=?
            WHERE id=? AND status='running'
            """,
            (progress, utc_now_iso(), job_id),
        )
    return "\n".join(stderr_tail)


class ClipWorker:
    def __init__(self, database: Database = db) -> None:
        self.db = database
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._process: asyncio.subprocess.Process | None = None
        self._current_job_id: int | None = None
        self._job_lock = asyncio.Lock()

    def start(self) -> None:
        if not self._task:
            self._stop.clear()
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._process and self._process.returncode is None:
            await terminate_process(self._process)
        if self._task:
            await self._task
        self._task = None

    async def create_job(
        self,
        media_id: int,
        start_seconds: int,
        end_seconds: int,
    ) -> dict[str, Any]:
        async with self._job_lock:
            return await self._create_job(media_id, start_seconds, end_seconds)

    async def _create_job(
        self,
        media_id: int,
        start_seconds: int,
        end_seconds: int,
    ) -> dict[str, Any]:
        item = self.db.get_media_item(media_id)
        if not item or item.get("status") != "available":
            raise KeyError("Media not found")
        source_path = Path(str(item["video_path"]))
        if not within_final_root(source_path) or not source_path.is_file():
            raise FileNotFoundError("Media file not found")
        if start_seconds < 0 or end_seconds <= start_seconds:
            raise ValueError("End time must be at least one second after start time")
        if end_seconds - start_seconds < 1:
            raise ValueError("Clip must be at least one second long")

        duration = item.get("duration_seconds")
        if not duration:
            duration = await probe_duration_seconds(source_path)
            if duration:
                self.db.execute(
                    "UPDATE media_items SET duration_seconds=?, updated_at=? WHERE id=?",
                    (duration, utc_now_iso(), media_id),
                )
        if not duration:
            raise ValueError("Could not determine media duration")
        if start_seconds >= float(duration) or end_seconds > float(duration):
            raise ValueError("Clip range must be within the original video")

        now = datetime.now(timezone.utc)
        reusable = self.db.query_one(
            """
            SELECT * FROM clip_jobs
            WHERE media_id=? AND start_seconds=? AND end_seconds=?
              AND status='completed' AND expires_at > ?
            ORDER BY id DESC LIMIT 1
            """,
            (media_id, start_seconds, end_seconds, now.isoformat()),
        )
        if reusable and reusable.get("output_path"):
            output = Path(str(reusable["output_path"]))
            if output.is_file() and output.stat().st_size > 0:
                return reusable

        config.CLIP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        estimated_size = max(
            1,
            int(
                int(item.get("size_bytes") or source_path.stat().st_size)
                * (end_seconds - start_seconds)
                / float(duration)
            ),
        )
        free = shutil.disk_usage(config.CLIP_TEMP_DIR).free
        reserved = int(
            (
                self.db.query_one(
                    """
                    SELECT COALESCE(sum(estimated_size_bytes), 0) AS bytes
                    FROM clip_jobs WHERE status IN ('queued','running')
                    """
                )
                or {}
            ).get("bytes")
            or 0
        )
        if free - reserved - estimated_size < config.CLIP_MIN_FREE_BYTES:
            raise InsufficientClipStorageError(
                "Not enough temporary disk space while preserving the 100 GiB safety margin"
            )

        created_at = now.isoformat()
        download_name = build_clip_download_name(
            source_path, start_seconds, end_seconds
        )
        cursor = self.db.execute(
            """
            INSERT INTO clip_jobs
              (media_id, source_title, start_seconds, end_seconds, download_name,
               estimated_size_bytes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                media_id,
                str(item["title"]),
                start_seconds,
                end_seconds,
                download_name,
                estimated_size,
                created_at,
                created_at,
            ),
        )
        job_id = int(cursor.lastrowid)
        output_path = config.CLIP_TEMP_DIR / f"clip-{job_id}.mp4"
        self.db.execute(
            "UPDATE clip_jobs SET output_path=?, updated_at=? WHERE id=?",
            (str(output_path), utc_now_iso(), job_id),
        )
        latest = self.db.get_media_item(media_id)
        if (
            not latest
            or latest.get("status") != "available"
            or not source_path.is_file()
        ):
            now_text = utc_now_iso()
            self.db.execute(
                """
                UPDATE clip_jobs
                SET status='canceled', error='Source entered retention trash',
                    finished_at=?, updated_at=?
                WHERE id=?
                """,
                (now_text, now_text, job_id),
            )
            raise FileNotFoundError("Media entered retention trash before the clip was queued")
        logger.info(
            "Clip queued: media %s, %ss-%ss", media_id, start_seconds, end_seconds
        )
        return self.get_job(job_id) or {}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self.db.query_one(
            """
            SELECT clip_jobs.*, media_items.channel_name
            FROM clip_jobs
            LEFT JOIN media_items ON media_items.id = clip_jobs.media_id
            WHERE clip_jobs.id=?
            """,
            (job_id,),
        )

    def jobs(self, media_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...]
        if media_id is None:
            params = (limit,)
        else:
            where = "WHERE clip_jobs.media_id=?"
            params = (media_id, limit)
        return self.db.query_all(
            f"""
            SELECT clip_jobs.*, media_items.channel_name
            FROM clip_jobs
            LEFT JOIN media_items ON media_items.id = clip_jobs.media_id
            {where}
            ORDER BY clip_jobs.id DESC LIMIT ?
            """,
            params,
        )

    async def cancel_job(self, job_id: int) -> bool:
        job = self.get_job(job_id)
        if not job or job.get("status") not in {"queued", "running"}:
            return False
        self.db.execute(
            """
            UPDATE clip_jobs SET status='canceled', finished_at=?, updated_at=?
            WHERE id=? AND status IN ('queued','running')
            """,
            (utc_now_iso(), utc_now_iso(), job_id),
        )
        if self._current_job_id == job_id and self._process:
            await terminate_process(self._process)
        self._unlink_job_files(job)
        logger.info("Clip canceled: %s", job_id)
        return True

    def delete_result(self, job_id: int) -> bool:
        job = self.get_job(job_id)
        if not job or job.get("status") not in {"completed", "expired"}:
            return False
        self._unlink_job_files(job)
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE clip_jobs
            SET status='deleted', expires_at=NULL, updated_at=?
            WHERE id=?
            """,
            (now, job_id),
        )
        logger.info("Clip result deleted: %s", job_id)
        return True

    def recover_interrupted_jobs(self) -> dict[str, int]:
        recovered = {"queued": 0, "completed": 0, "failed": 0}
        rows = self.db.query_all("SELECT * FROM clip_jobs WHERE status='running'")
        for job in rows:
            output_path = (
                Path(str(job["output_path"])) if job.get("output_path") else None
            )
            part_path = self._part_path(output_path) if output_path else None
            if output_path and output_path.is_file() and output_path.stat().st_size > 0:
                now = datetime.now(timezone.utc)
                self.db.execute(
                    """
                    UPDATE clip_jobs
                    SET status='completed', progress_percent=100, finished_at=?,
                        expires_at=?, output_size_bytes=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        now.isoformat(),
                        (
                            now
                            + timedelta(hours=config.CLIP_RESULT_RETENTION_HOURS)
                        ).isoformat(),
                        output_path.stat().st_size,
                        now.isoformat(),
                        int(job["id"]),
                    ),
                )
                recovered["completed"] += 1
                continue
            if part_path:
                part_path.unlink(missing_ok=True)
            media = (
                self.db.get_media_item(int(job["media_id"]))
                if job.get("media_id") is not None
                else None
            )
            source_ok = bool(
                media
                and media.get("status") == "available"
                and Path(str(media["video_path"])).is_file()
            )
            if int(job.get("retry_count") or 0) < 1 and source_ok:
                self.db.execute(
                    """
                    UPDATE clip_jobs
                    SET status='queued', retry_count=retry_count+1,
                        progress_percent=0, error=NULL, started_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (utc_now_iso(), int(job["id"])),
                )
                recovered["queued"] += 1
            else:
                self._mark_failed(
                    int(job["id"]),
                    "Clip generation was interrupted and could not be retried",
                )
                recovered["failed"] += 1
        return recovered

    def cleanup_expired(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        expired = 0
        rows = self.db.query_all(
            """
            SELECT * FROM clip_jobs
            WHERE status='completed' AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (now.isoformat(),),
        )
        for row in rows:
            self._unlink_job_files(row)
            self.db.execute(
                "UPDATE clip_jobs SET status='expired', updated_at=? WHERE id=?",
                (now.isoformat(), int(row["id"])),
            )
            expired += 1
        cutoff = now - timedelta(days=config.CLIP_JOB_HISTORY_DAYS)
        cursor = self.db.execute(
            """
            DELETE FROM clip_jobs
            WHERE status IN ('failed','canceled','expired','deleted')
              AND COALESCE(finished_at, updated_at) < ?
            """,
            (cutoff.isoformat(),),
        )
        return {"expired": expired, "records_deleted": max(0, cursor.rowcount)}

    async def run(self) -> None:
        logger.info("Clip worker started")
        while not self._stop.is_set():
            job = self.db.query_one(
                "SELECT * FROM clip_jobs WHERE status='queued' ORDER BY id LIMIT 1"
            )
            if not job:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                continue
            await self._run_job(job)

    async def _run_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        media_id = int(job["media_id"]) if job.get("media_id") is not None else None
        media = self.db.get_media_item(media_id) if media_id is not None else None
        if not media or media.get("status") != "available":
            self._mark_failed(job_id, "Source media is unavailable")
            return
        source_path = Path(str(media["video_path"]))
        output_path = Path(str(job["output_path"]))
        part_path = self._part_path(output_path)
        if not within_final_root(source_path) or not source_path.is_file():
            self._mark_failed(job_id, "Source media file is missing")
            return
        config.CLIP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        part_path.unlink(missing_ok=True)
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE clip_jobs
            SET status='running', progress_percent=0, error=NULL,
                started_at=?, updated_at=?
            WHERE id=? AND status='queued'
            """,
            (now, now, job_id),
        )
        self._current_job_id = job_id
        logger.info("Clip generation started: %s", job_id)
        try:
            command = build_clip_command(
                source_path,
                part_path,
                int(job["start_seconds"]),
                int(job["end_seconds"]),
            )
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
            progress_task = (
                asyncio.create_task(
                    read_clip_progress(
                        self._process.stderr,
                        self.db,
                        job_id,
                        int(job["end_seconds"]) - int(job["start_seconds"]),
                    )
                )
                if self._process.stderr
                else None
            )
            returncode = await self._process.wait()
            stderr = await progress_task if progress_task else ""
            current = self.get_job(job_id) or {}
            if current.get("status") == "canceled":
                part_path.unlink(missing_ok=True)
                return
            if returncode != 0:
                if self._stop.is_set():
                    self._requeue_after_shutdown(job_id, part_path)
                    return
                raise RuntimeError(f"ffmpeg exited {returncode}: {stderr[-2000:]}")
            if not part_path.is_file() or part_path.stat().st_size == 0:
                raise RuntimeError("ffmpeg completed but clip output is missing")
            part_path.replace(output_path)
            finished = datetime.now(timezone.utc)
            self.db.execute(
                """
                UPDATE clip_jobs
                SET status='completed', progress_percent=100, finished_at=?,
                    expires_at=?, output_size_bytes=?, updated_at=?
                WHERE id=?
                """,
                (
                    finished.isoformat(),
                    (
                        finished + timedelta(hours=config.CLIP_RESULT_RETENTION_HOURS)
                    ).isoformat(),
                    output_path.stat().st_size,
                    finished.isoformat(),
                    job_id,
                ),
            )
            logger.info("Clip generation completed: %s", job_id)
        except Exception as exc:
            part_path.unlink(missing_ok=True)
            self._mark_failed(job_id, str(exc))
            logger.exception("Clip generation failed for job %s: %s", job_id, exc)
        finally:
            self._process = None
            self._current_job_id = None

    def _requeue_after_shutdown(self, job_id: int, part_path: Path) -> None:
        part_path.unlink(missing_ok=True)
        job = self.get_job(job_id) or {}
        if int(job.get("retry_count") or 0) < 1:
            self.db.execute(
                """
                UPDATE clip_jobs
                SET status='queued', retry_count=retry_count+1,
                    progress_percent=0, started_at=NULL, updated_at=?
                WHERE id=?
                """,
                (utc_now_iso(), job_id),
            )
            logger.warning("Clip interrupted by shutdown; requeued job %s", job_id)
        else:
            self._mark_failed(job_id, "Clip generation was interrupted twice")

    def _mark_failed(self, job_id: int, error: str) -> None:
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE clip_jobs
            SET status='failed', error=?, finished_at=?, updated_at=?
            WHERE id=? AND status != 'canceled'
            """,
            (error[-2000:], now, now, job_id),
        )

    def _unlink_job_files(self, job: dict[str, Any]) -> None:
        if not job.get("output_path"):
            return
        output_path = Path(str(job["output_path"]))
        try:
            output_path.resolve().relative_to(config.CLIP_TEMP_DIR.resolve())
        except (OSError, ValueError):
            raise ValueError(f"Unsafe clip output path: {output_path}")
        output_path.unlink(missing_ok=True)
        self._part_path(output_path).unlink(missing_ok=True)

    @staticmethod
    def _part_path(output_path: Path) -> Path:
        return output_path.with_suffix(".mp4.part")

    def status(self) -> dict[str, Any]:
        queued = self.db.query_one(
            "SELECT count(*) AS count FROM clip_jobs WHERE status='queued'"
        ) or {}
        return {
            "running": bool(self._task and not self._task.done()),
            "current_job_id": self._current_job_id,
            "queued": int(queued.get("count") or 0),
        }


clip_worker = ClipWorker()
