from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .chzzk_api import get_live_detail, streamlink_header_args
from .chat_capture import ChatCapture
from .db import db
from .logger import logger
from .utils import ensure_storage_dirs, kst_iso, now_kst, sanitize_name, shorten_filename, unique_path


@dataclass
class ActiveRecording:
    session_id: int
    channel_id: str
    channel_name: str
    stop_event: asyncio.Event
    stream_process: asyncio.subprocess.Process | None = None
    ffmpeg_process: asyncio.subprocess.Process | None = None


def recording_name(started_at: datetime, streamer: str, title: str, suffix: str) -> str:
    safe_streamer = sanitize_name(streamer, "unknown")
    safe_title = sanitize_name(title, "untitled")
    stamp = started_at.strftime("%y%m%d %H-%M-%S")
    return shorten_filename(f"[{stamp}] {safe_streamer} - {safe_title}{suffix}")


async def terminate_process(process: asyncio.subprocess.Process | None, name: str) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=10)
    except Exception:
        logger.warning("%s did not stop cleanly; killing it", name)
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


class RecorderSupervisor:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._channel_tasks: dict[str, asyncio.Task] = {}
        self.active: dict[int, ActiveRecording] = {}

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        for active in list(self.active.values()):
            active.stop_event.set()
            await terminate_process(active.ffmpeg_process, "ffmpeg")
            await terminate_process(active.stream_process, "streamlink")
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
        await terminate_process(active.stream_process, "streamlink")
        await terminate_process(active.ffmpeg_process, "ffmpeg")
        logger.info("Manual stop requested for session %s", session_id)
        return True

    async def run(self) -> None:
        logger.info("Recorder supervisor started")
        while not self._stop.is_set():
            active_channels = {channel["id"]: channel for channel in db.get_channels(active_only=True)}

            for channel_id in list(self._channel_tasks):
                if channel_id not in active_channels:
                    self._channel_tasks[channel_id].cancel()
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
            try:
                live = await get_live_detail(channel_id, tokens)
                if not live or live.get("status") != "OPEN":
                    await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
                    continue
                await self.record_once(channel, live, tokens)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Recording loop error for %s: %s", channel_id, exc)
                await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

    async def record_once(self, channel: dict[str, Any], live: dict[str, Any], tokens: dict[str, str]) -> None:
        channel_id = str(channel["id"])
        channel_name = str(channel["name"])
        live_id = str(live.get("liveId") or "")
        live_title = str(live.get("liveTitle") or "untitled")
        started_at = now_kst()

        video_dir, chat_dir = ensure_storage_dirs(channel_name)
        config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

        base = recording_name(started_at, channel_name, live_title, "")
        temp_path = unique_path(config.TEMP_DIR / f"{base}.ts.part")
        source_path = temp_path.with_suffix("")
        final_path = unique_path(video_dir / f"{base}.mp4")
        chat_jsonl_path = unique_path(chat_dir / f"{base}.jsonl")
        chat_csv_path = unique_path(chat_jsonl_path.with_suffix(".csv"))

        session_id = db.create_session(
            channel_id,
            channel_name,
            live_id,
            live_title,
            kst_iso(started_at),
            temp_path,
            chat_jsonl_path,
            chat_csv_path,
        )
        stop_event = asyncio.Event()
        active = ActiveRecording(session_id, channel_id, channel_name, stop_event)
        self.active[session_id] = active
        logger.info("Recording started for %s: %s", channel_name, live_title)

        chat_capture = ChatCapture(channel_id, tokens, chat_jsonl_path, chat_csv_path, started_at)
        chat_task = asyncio.create_task(chat_capture.run(stop_event.is_set))

        streamlink = shutil.which("streamlink")
        ffmpeg = shutil.which("ffmpeg")
        if not streamlink or not ffmpeg:
            raise RuntimeError("streamlink or ffmpeg is not installed")

        stream_url = f"https://chzzk.naver.com/live/{channel_id}"
        streamlink_cmd = [
            streamlink,
            "--stdout",
            stream_url,
            "best",
            "--hls-live-restart",
            "--plugin-dirs",
            str(config.STREAMLINK_PLUGIN_DIR),
            "--stream-segment-threads",
            "2",
            *streamlink_header_args(tokens),
            "--ffmpeg-ffmpeg",
            ffmpeg,
            "--ffmpeg-copyts",
            "--hls-segment-stream-data",
        ]
        ffmpeg_cmd = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            "pipe:0",
            "-map",
            "0",
            "-c",
            "copy",
            "-copy_unknown",
            "-f",
            "mpegts",
            "-mpegts_flags",
            "resend_headers",
            str(temp_path),
        ]

        try:
            active.stream_process = await asyncio.create_subprocess_exec(
                *streamlink_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
            active.ffmpeg_process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
            if active.stream_process.stdout is None or active.ffmpeg_process.stdin is None:
                raise RuntimeError("recording process pipes were not created")

            tasks = [
                asyncio.create_task(pipe_stream(active.stream_process.stdout, active.ffmpeg_process.stdin)),
                asyncio.create_task(log_process_stderr(active.stream_process.stderr, f"streamlink {channel_id}")),
                asyncio.create_task(log_process_stderr(active.ffmpeg_process.stderr, f"ffmpeg {channel_id}")),
                asyncio.create_task(active.stream_process.wait()),
                asyncio.create_task(active.ffmpeg_process.wait()),
                asyncio.create_task(stop_event.wait()),
            ]
            done, pending = await asyncio.wait(tasks[3:], return_when=asyncio.FIRST_COMPLETED)
            if tasks[5] in done:
                await terminate_process(active.stream_process, "streamlink")
                await terminate_process(active.ffmpeg_process, "ffmpeg")
            elif tasks[3] in done:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(tasks[4], timeout=30)
                await terminate_process(active.ffmpeg_process, "ffmpeg")
            elif tasks[4] in done:
                await terminate_process(active.stream_process, "streamlink")

            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            if temp_path.exists() and temp_path.stat().st_size > 0:
                source_path = unique_path(source_path)
                temp_path.replace(source_path)
                db.finish_session(session_id, source_path, "queued")
                db.add_encode_job(session_id, source_path, final_path)
                logger.info("Recording queued for encoding: %s", source_path)
            else:
                db.finish_session(session_id, None, "failed")
                db.update_session_status(session_id, "failed", error="No recording file was created")
                logger.warning("Recording produced no output for %s", channel_name)
        except Exception as exc:
            db.update_session_status(session_id, "failed", error=str(exc))
            logger.exception("Recording failed for %s: %s", channel_name, exc)
        finally:
            stop_event.set()
            chat_task.cancel()
            await asyncio.gather(chat_task, return_exceptions=True)
            await terminate_process(active.ffmpeg_process, "ffmpeg")
            await terminate_process(active.stream_process, "streamlink")
            self.active.pop(session_id, None)
