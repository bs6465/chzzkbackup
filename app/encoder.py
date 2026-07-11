from __future__ import annotations

import asyncio
import os
import signal
import shlex
from collections import deque
from pathlib import Path

from . import config
from .db import db
from .logger import logger


def parse_ffmpeg_seconds(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return max(0.0, int(value) / 1_000_000)
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
    except ValueError:
        return None
    return max(0.0, hours * 3600 + minutes * 60 + seconds)


def parse_speed_factor(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value.strip().rstrip("x"))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def calculate_progress(
    duration_seconds: float | None,
    encoded_seconds: float | None,
    speed: str | None,
) -> tuple[float | None, float | None]:
    if duration_seconds is None or duration_seconds <= 0 or encoded_seconds is None:
        return None, None
    progress = min(99.9, max(0.0, encoded_seconds / duration_seconds * 100))
    speed_factor = parse_speed_factor(speed)
    eta = None
    if speed_factor:
        eta = max(0.0, (duration_seconds - encoded_seconds) / speed_factor)
    return progress, eta


def build_x264_mp4_command(source_path: Path, final_path: Path) -> list[str]:
    input_args = ["-f", "concat", "-safe", "0", "-i", str(source_path)] if source_path.suffix == ".concat" else ["-i", str(source_path)]
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-nostats",
        "-progress",
        "pipe:2",
        *input_args,
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


async def probe_duration_seconds(source_path: Path) -> float | None:
    if source_path.suffix == ".concat":
        durations = [await probe_duration_seconds(path) for path in read_concat_sources(source_path)]
        valid = [value for value in durations if value]
        return sum(valid) if valid else None
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None
    try:
        duration = float(stdout.decode().strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def read_concat_sources(manifest: Path) -> list[Path]:
    if not manifest.exists():
        return []
    paths: list[Path] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.startswith("file "):
            continue
        try:
            value = shlex.split(line)[1]
        except (ValueError, IndexError):
            continue
        paths.append(Path(value))
    return paths


async def read_encode_progress(
    stream: asyncio.StreamReader,
    job_id: int,
    duration_seconds: float | None,
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

        encoded_seconds = (
            parse_ffmpeg_seconds(values.get("out_time_us", ""))
            or parse_ffmpeg_seconds(values.get("out_time_ms", ""))
            or parse_ffmpeg_seconds(values.get("out_time", ""))
        )
        speed = values.get("speed")
        progress_percent, eta_seconds = calculate_progress(duration_seconds, encoded_seconds, speed)
        if value == "end":
            progress_percent = 100.0
            eta_seconds = 0.0
        db.update_encode_progress(
            job_id,
            duration_seconds=duration_seconds,
            encoded_seconds=encoded_seconds,
            progress_percent=progress_percent,
            speed=speed,
            eta_seconds=eta_seconds,
        )
    return "\n".join(stderr_tail)


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
        self._process: asyncio.subprocess.Process | None = None

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._process and self._process.returncode is None:
            await terminate_process(self._process)
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
                duration_seconds = await probe_duration_seconds(source_path)
                db.update_encode_progress(
                    job_id,
                    duration_seconds=duration_seconds,
                    encoded_seconds=0,
                    progress_percent=0,
                    speed=None,
                    eta_seconds=None,
                )
                cmd = build_x264_mp4_command(source_path, final_path)
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name != "nt"),
                )
                progress_task = None
                if self._process.stderr:
                    progress_task = asyncio.create_task(read_encode_progress(self._process.stderr, job_id, duration_seconds))
                returncode = await self._process.wait()
                stderr = await progress_task if progress_task else ""
                if returncode != 0:
                    error = stderr[-2000:]
                    if self._stop.is_set() and source_path.exists():
                        db.requeue_encode_job(job_id, session_id)
                        logger.warning("Encoding interrupted by shutdown; requeued job %s", job_id)
                        continue
                    raise RuntimeError(f"ffmpeg exited {returncode}: {error}")

                if not final_path.exists() or final_path.stat().st_size == 0:
                    raise RuntimeError("ffmpeg completed but final file is missing or empty")

                final_path = db.finalize_encode_output(job_id, final_path)
                if source_path.suffix == ".concat":
                    for segment_path in read_concat_sources(source_path):
                        segment_path.unlink(missing_ok=True)
                source_path.unlink(missing_ok=True)
                db.update_encode_job(job_id, "completed")
                db.update_session_status(session_id, "completed", final_path=final_path)
                session = db.get_session(session_id) or {}
                db.upsert_media_item(
                    video_path=final_path,
                    session_id=session_id,
                    platform=str(session.get("platform") or "chzzk"),
                    channel_name=str(session.get("channel_name") or final_path.parent.name),
                    title=str(session.get("live_title") or final_path.stem),
                    started_at=str(session.get("started_at") or session.get("ended_at") or ""),
                    chat_jsonl_path=Path(session["chat_jsonl_path"]) if session.get("chat_jsonl_path") else None,
                    chat_csv_path=Path(session["chat_csv_path"]) if session.get("chat_csv_path") else None,
                    duration_seconds=duration_seconds,
                    size_bytes=final_path.stat().st_size,
                )
                logger.info("Encoding completed: %s", final_path)
            except Exception as exc:
                retry_count = int(job.get("retry_count") or 0)
                if retry_count < len(config.ENCODE_RETRY_DELAYS) and source_path.exists():
                    delay = config.ENCODE_RETRY_DELAYS[retry_count]
                    attempt = db.schedule_encode_retry(job_id, session_id, str(exc), delay)
                    logger.warning(
                        "Encoding failed for job %s; automatic retry %s/%s in %ss: %s",
                        job_id, attempt, len(config.ENCODE_RETRY_DELAYS), delay, exc,
                    )
                else:
                    db.update_encode_job(job_id, "failed", str(exc))
                    db.update_session_status(session_id, "failed", error=str(exc))
                    logger.exception("Encoding failed for job %s after retries: %s", job_id, exc)
            finally:
                self._process = None
