from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import shutil
import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from . import config
from .db import db
from .logger import logger
from .platforms import create_chat_capture, get_live_info
from .chat_csv_migrate import STANDARD_CSV_FIELDS
from .utils import ensure_storage_dirs, kst_iso, now_kst, recording_name, unique_path


@dataclass
class ActiveRecording:
    session_id: int
    channel_id: str
    channel_name: str
    stop_event: asyncio.Event
    stream_process: asyncio.subprocess.Process | None = None
    ffmpeg_process: asyncio.subprocess.Process | None = None


async def terminate_process(
    process: asyncio.subprocess.Process | None,
    name: str,
    *,
    warn_on_kill: bool = True,
) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=10)
    except Exception:
        log_func = logger.warning if warn_on_kill else logger.info
        log_func("%s did not stop cleanly; killing it", name)
        with contextlib.suppress(Exception):
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        with contextlib.suppress(Exception):
            await process.wait()


async def log_process_stderr(stream: asyncio.StreamReader | None, prefix: str) -> None:
    if stream is None:
        return
    while not stream.at_eof():
        line = await stream.readline()
        if not line:
            break
        text = line.decode(errors="replace").strip()
        if text:
            logger.info("%s: %s", prefix, text)


async def pipe_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(256 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        if not writer.is_closing():
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def build_streamlink_command(streamlink: str, ffmpeg: str, live: Any) -> list[str]:
    return [
        streamlink,
        "--stdout",
        live.stream_url,
        live.stream_name,
        "--hls-live-restart",
        "--stream-segment-threads",
        "2",
        *live.streamlink_args,
        "--ffmpeg-ffmpeg",
        ffmpeg,
        "--ffmpeg-copyts",
        "--hls-segment-stream-data",
    ]


NETWORK_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
)


class RecorderSupervisor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._channel_tasks: dict[str, asyncio.Task] = {}
        self._network_error_log_at: dict[str, float] = {}
        self.active: dict[int, ActiveRecording] = {}

    def _should_log_network_error(self, channel_id: str) -> bool:
        now = asyncio.get_running_loop().time()
        last_logged_at = self._network_error_log_at.get(channel_id)
        if (
            last_logged_at is None
            or now - last_logged_at >= config.NETWORK_ERROR_LOG_INTERVAL_SECONDS
        ):
            self._network_error_log_at[channel_id] = now
            return True
        return False

    def _clear_network_error(self, channel_id: str) -> None:
        self._network_error_log_at.pop(channel_id, None)

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        for active in list(self.active.values()):
            active.stop_event.set()
            await terminate_process(active.ffmpeg_process, "ffmpeg", warn_on_kill=False)
            await terminate_process(active.stream_process, "streamlink", warn_on_kill=False)
        for task in self._channel_tasks.values():
            task.cancel()
        if self._channel_tasks:
            await asyncio.gather(*self._channel_tasks.values(), return_exceptions=True)
        if self._task:
            await self._task

    async def stop_session(self, session_id: int) -> bool:
        active = self.active.get(session_id)
        if not active:
            return False
        active.stop_event.set()
        logger.info("Manual stop requested for session %s", session_id)
        await terminate_process(active.stream_process, "streamlink", warn_on_kill=False)
        await terminate_process(active.ffmpeg_process, "ffmpeg", warn_on_kill=False)
        return True

    async def run(self) -> None:
        logger.info("Recorder supervisor started")
        while not self._stop.is_set():
            active_channels = {channel["id"]: channel for channel in db.get_channels(active_only=True)}

            for channel_id in list(self._channel_tasks):
                if channel_id not in active_channels:
                    has_active_recording = False
                    for active in list(self.active.values()):
                        if active.channel_id == channel_id:
                            has_active_recording = True
                            active.stop_event.set()
                            await terminate_process(active.stream_process, "streamlink", warn_on_kill=False)
                            await terminate_process(active.ffmpeg_process, "ffmpeg", warn_on_kill=False)
                    task = self._channel_tasks[channel_id]
                    if has_active_recording and not task.done():
                        continue
                    task.cancel()
                    self._channel_tasks.pop(channel_id, None)

            for channel_id, channel in active_channels.items():
                task = self._channel_tasks.get(channel_id)
                if task is None or task.done():
                    self._channel_tasks[channel_id] = asyncio.create_task(self.channel_loop(channel_id))

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    async def channel_loop(self, channel_id: str) -> None:
        while not self._stop.is_set():
            channel = db.get_channel(channel_id)
            if not channel or not channel["active"]:
                return

            tokens = db.get_tokens()
            twitcasting_token = db.get_twitcasting_token()
            try:
                live = await get_live_info(channel, tokens, twitcasting_token)
            except asyncio.CancelledError:
                raise
            except NETWORK_ERRORS as exc:
                if self._should_log_network_error(channel_id):
                    logger.warning(
                        "Temporary network error while polling %s; retrying in %ss: %s",
                        channel_id,
                        config.POLL_INTERVAL_SECONDS,
                        exc,
                    )
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                continue
            except Exception as exc:
                logger.exception("Recording loop error for %s: %s", channel_id, exc)
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                continue

            self._clear_network_error(channel_id)
            try:
                if not live:
                    await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                    continue
                await self.record_once(channel, live, tokens, twitcasting_token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Recording loop error for %s: %s", channel_id, exc)
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

    async def record_once(
        self,
        channel: dict[str, Any],
        live: Any,
        tokens: dict[str, str],
        twitcasting_token: str = "",
    ) -> None:
        channel_id = str(channel["id"])
        channel_display_id = str(channel.get("display_id") or channel_id)
        platform = str(channel.get("platform") or "chzzk")
        channel_name = str(channel["name"])
        live_id = live.live_id
        live_title = live.title
        started_at = now_kst()

        video_dir, chat_dir = ensure_storage_dirs(channel_name, platform)
        config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

        base = recording_name(started_at, channel_name, live_title, "")
        temp_path = unique_path(config.TEMP_DIR / f"{base}.segment-001.ts.part")
        final_path = unique_path(video_dir / f"{base}.mp4")
        chat_jsonl_temp_path = unique_path(config.TEMP_DIR / f"{base}.segment-001.chat.jsonl.part")
        chat_csv_temp_path = unique_path(config.TEMP_DIR / f"{base}.segment-001.chat.csv.part")
        chat_jsonl_path = chat_dir / f"{base}.jsonl"
        chat_csv_path = chat_dir / f"{base}.csv"

        session_id = db.create_session(
            channel_id,
            channel_name,
            live_id,
            live_title,
            kst_iso(started_at),
            temp_path,
            chat_jsonl_path,
            chat_csv_path,
            chat_jsonl_temp_path,
            chat_csv_temp_path,
            final_path,
            platform,
            channel_display_id,
        )
        stop_event = asyncio.Event()
        active = ActiveRecording(session_id, channel_id, channel_name, stop_event)
        self.active[session_id] = active
        logger.info("Recording started for %s %s: %s", platform, channel_name, live_title)
        sequence = 0
        retry_count = 0
        offline_count = 0
        current_live = live
        try:
            while not stop_event.is_set():
                sequence += 1
                segment_started = now_kst()
                segment_base = f"{base}.segment-{sequence:03d}"
                segment_temp = config.TEMP_DIR / f"{segment_base}.ts.part"
                segment_jsonl = config.TEMP_DIR / f"{segment_base}.chat.jsonl.part"
                segment_csv = config.TEMP_DIR / f"{segment_base}.chat.csv.part"
                db.execute(
                    """UPDATE recording_sessions
                       SET temp_path=?, chat_jsonl_temp_path=?, chat_csv_temp_path=?, status='recording'
                       WHERE id=?""",
                    (str(segment_temp), str(segment_jsonl), str(segment_csv), session_id),
                )
                source, error = await self._capture_segment(
                    active, channel, current_live, tokens, twitcasting_token,
                    segment_temp, segment_jsonl, segment_csv, segment_started,
                )
                final_segment_jsonl = self._finish_part(segment_jsonl)
                final_segment_csv = self._finish_part(segment_csv, csv_file=True)
                duration = await self._probe_duration(source) if source else None
                db.add_recording_segment(
                    session_id, sequence, source, final_segment_jsonl, final_segment_csv,
                    kst_iso(segment_started), duration_seconds=duration,
                    has_video=bool(source), error=error,
                )
                if source:
                    retry_count = 0
                    db.execute(
                        "UPDATE recording_sessions SET status='recording', retry_count=0, next_retry_at=NULL, error=NULL WHERE id=?",
                        (session_id,),
                    )
                else:
                    retry_count += 1
                    delay = config.RECORDING_RETRY_DELAYS[min(retry_count - 1, len(config.RECORDING_RETRY_DELAYS) - 1)]
                    db.update_recording_retry(session_id, retry_count, delay, error or "No recording file was created")
                    logger.warning("Recording produced no output for %s; retrying in %ss", channel_name, delay)

                while not stop_event.is_set():
                    await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                    try:
                        latest = await get_live_info(channel, db.get_tokens(), db.get_twitcasting_token())
                    except NETWORK_ERRORS as exc:
                        if self._should_log_network_error(channel_id):
                            logger.warning("Temporary network error while confirming broadcast end for %s: %s", channel_id, exc)
                        continue
                    if latest and latest.live_id == live_id:
                        offline_count = 0
                        current_live = latest
                        if not source:
                            delay = config.RECORDING_RETRY_DELAYS[min(retry_count - 1, len(config.RECORDING_RETRY_DELAYS) - 1)]
                            await self._sleep_until_stop(stop_event, max(0, delay - config.POLL_INTERVAL_SECONDS))
                        break
                    offline_count += 1
                    if offline_count >= config.OFFLINE_CONFIRMATION_COUNT:
                        stop_event.set()
                        break

            await self._finalize_logical_session(session_id, final_path, chat_jsonl_path, chat_csv_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            db.update_session_status(session_id, "failed", error=str(exc))
            logger.exception("Recording failed for %s: %s", channel_name, exc)
        finally:
            stop_event.set()
            await terminate_process(active.ffmpeg_process, "ffmpeg", warn_on_kill=False)
            await terminate_process(active.stream_process, "streamlink", warn_on_kill=False)
            self.active.pop(session_id, None)

    async def _capture_segment(
        self, active: ActiveRecording, channel: dict[str, Any], live: Any,
        tokens: dict[str, str], twitcasting_token: str, temp_path: Path,
        chat_jsonl_path: Path, chat_csv_path: Path, started_at: datetime,
    ) -> tuple[Path | None, str | None]:
        streamlink = shutil.which("streamlink")
        ffmpeg = shutil.which("ffmpeg")
        if not streamlink or not ffmpeg:
            raise RuntimeError("streamlink or ffmpeg is not installed")
        segment_stop = asyncio.Event()
        chat_capture = create_chat_capture(
            channel, live, tokens, twitcasting_token, chat_jsonl_path, chat_csv_path, started_at,
        )
        chat_task = asyncio.create_task(chat_capture.run(segment_stop.is_set)) if chat_capture else None
        error: str | None = None
        try:
            active.stream_process = await asyncio.create_subprocess_exec(
                *build_streamlink_command(streamlink, ffmpeg, live), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=(os.name != "nt"),
            )
            active.ffmpeg_process = await asyncio.create_subprocess_exec(
                ffmpeg, "-hide_banner", "-y", "-i", "pipe:0", "-map", "0", "-c", "copy",
                "-copy_unknown", "-f", "mpegts", "-mpegts_flags", "resend_headers", str(temp_path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, start_new_session=(os.name != "nt"),
            )
            if active.stream_process.stdout is None or active.ffmpeg_process.stdin is None:
                raise RuntimeError("recording process pipes were not created")
            pipe_task = asyncio.create_task(pipe_stream(active.stream_process.stdout, active.ffmpeg_process.stdin))
            stream_log = asyncio.create_task(log_process_stderr(active.stream_process.stderr, "streamlink"))
            ffmpeg_log = asyncio.create_task(log_process_stderr(active.ffmpeg_process.stderr, "ffmpeg"))
            waits = [
                asyncio.create_task(active.stream_process.wait()),
                asyncio.create_task(active.ffmpeg_process.wait()),
                asyncio.create_task(active.stop_event.wait()),
            ]
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if waits[2] in done:
                await terminate_process(active.stream_process, "streamlink", warn_on_kill=False)
                await terminate_process(active.ffmpeg_process, "ffmpeg", warn_on_kill=False)
            elif waits[0] in done:
                await terminate_process(active.ffmpeg_process, "ffmpeg")
            else:
                await terminate_process(active.stream_process, "streamlink")
            for task in waits:
                if not task.done(): task.cancel()
            await asyncio.gather(pipe_task, stream_log, ffmpeg_log, *waits, return_exceptions=True)
        except Exception as exc:
            error = str(exc)
            logger.warning("Recording segment failed: %s", exc)
        finally:
            segment_stop.set()
            if chat_task:
                chat_task.cancel()
                await asyncio.gather(chat_task, return_exceptions=True)
        if temp_path.exists() and temp_path.stat().st_size > 0:
            source = unique_path(temp_path.with_suffix(""))
            temp_path.replace(source)
            return source, error
        temp_path.unlink(missing_ok=True)
        return None, error or "No recording file was created"

    async def _probe_duration(self, path: Path) -> float | None:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return None
        try:
            return float(stdout.decode().strip()) if process.returncode == 0 else None
        except ValueError:
            return None

    def _finish_part(self, path: Path, *, csv_file: bool = False) -> Path | None:
        if not path.exists() or path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            return None
        if csv_file:
            with path.open("r", encoding="utf-8-sig", errors="ignore") as handle:
                if sum(1 for line in handle if line.strip()) <= 1:
                    path.unlink(missing_ok=True)
                    return None
        target = path.with_suffix("")
        path.replace(target)
        return target

    async def _sleep_until_stop(self, stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _finalize_logical_session(
        self, session_id: int, final_path: Path, chat_jsonl_path: Path, chat_csv_path: Path,
    ) -> None:
        session = db.get_session(session_id) or {}
        final_path = Path(session.get("final_path") or final_path)
        chat_jsonl_path = Path(session.get("chat_jsonl_path") or chat_jsonl_path)
        chat_csv_path = Path(session.get("chat_csv_path") or chat_csv_path)
        segments = db.recording_segments(session_id)
        video_segments = [Path(row["source_path"]) for row in segments if row.get("has_video") and row.get("source_path")]
        self._merge_segment_chats(segments, chat_jsonl_path, chat_csv_path)
        if not video_segments:
            db.finish_session(session_id, None, "failed")
            db.update_session_status(session_id, "failed", error="No recording file was created")
            return
        if len(video_segments) == 1:
            source_path = video_segments[0]
        else:
            source_path = config.TEMP_DIR / f"session-{session_id}.concat"
            lines = ["ffconcat version 1.0"]
            for path in video_segments:
                escaped = str(path).replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        db.finish_session(session_id, source_path, "queued")
        db.add_encode_job(session_id, source_path, final_path)
        logger.info("Recording session %s queued with %s segment(s)", session_id, len(video_segments))

    def _merge_segment_chats(self, segments: list[dict[str, Any]], jsonl_path: Path, csv_path: Path) -> None:
        rows: list[dict[str, Any]] = []
        elapsed = 0.0
        for segment in segments:
            path = Path(segment["chat_jsonl_path"]) if segment.get("chat_jsonl_path") else None
            segment_rows: list[dict[str, Any]] = []
            if path and path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try: segment_rows.append(json.loads(line))
                        except json.JSONDecodeError: continue
            has_video = bool(segment.get("has_video"))
            for row in segment_rows:
                if has_video:
                    try: row["offset_seconds"] = round(elapsed + float(row.get("offset_seconds") or 0), 3)
                    except (TypeError, ValueError): row["offset_seconds"] = elapsed
                else:
                    row["offset_seconds"] = None
                rows.append(row)
            if has_video:
                elapsed += float(segment.get("duration_seconds") or 0)
        if rows:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with jsonl_path.open("w", encoding="utf-8") as handle:
                for row in rows: handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=STANDARD_CSV_FIELDS)
                writer.writeheader()
                for row in rows: writer.writerow({key: row.get(key, "") for key in STANDARD_CSV_FIELDS})
        for segment in segments:
            for key in ("chat_jsonl_path", "chat_csv_path"):
                if segment.get(key):
                    Path(segment[key]).unlink(missing_ok=True)
