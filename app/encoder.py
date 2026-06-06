from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from .db import db
from .logger import logger


def build_x264_mp4_command(source_path: Path, final_path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(final_path),
    ]


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=10)
    except Exception:
        if process.returncode is None:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
            await process.wait()


class EncodeWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def run(self) -> None:
        logger.info("Encode worker started")
        while not self._stop.is_set():
            job = db.next_encode_job()
            if not job:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                continue

            job_id = int(job["id"])
            session_id = int(job["session_id"])
            source_path = Path(job["source_path"])
            final_path = Path(job["final_path"])
            db.update_encode_job(job_id, "running")
            db.update_session_status(session_id, "encoding")
            logger.info("Encoding started: %s", final_path)

            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                cmd = build_x264_mp4_command(source_path, final_path)
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name != "nt"),
                )
                stderr = await process.stderr.read() if process.stderr else b""
                returncode = await process.wait()
                if returncode != 0:
                    error = stderr.decode(errors="replace")[-2000:]
                    raise RuntimeError(f"ffmpeg exited {returncode}: {error}")

                if not final_path.exists() or final_path.stat().st_size == 0:
                    raise RuntimeError("ffmpeg completed but final file is missing or empty")

                source_path.unlink(missing_ok=True)
                db.update_encode_job(job_id, "completed")
                db.update_session_status(session_id, "completed", final_path=final_path)
                logger.info("Encoding completed: %s", final_path)
            except Exception as exc:
                db.update_encode_job(job_id, "failed", str(exc))
                db.update_session_status(session_id, "failed", error=str(exc))
                logger.exception("Encoding failed for job %s: %s", job_id, exc)
