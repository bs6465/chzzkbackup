from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .db import db
from .logger import logger
from .utils import KST, recording_name, sanitize_name, utc_now_iso


MEDIA_NAME_RE = re.compile(
    r"^\[(?P<date>\d{6}) (?P<time>\d{2}-\d{2}-\d{2})\] (?P<channel>.+?) - (?P<title>.+)\.mp4$"
)


def within_final_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(config.FINAL_ROOT.resolve())
        return True
    except (OSError, ValueError):
        return False


def parse_media_filename(path: Path) -> dict[str, str]:
    match = MEDIA_NAME_RE.match(path.name)
    if not match:
        started = datetime.fromtimestamp(path.stat().st_mtime, tz=KST)
        return {
            "channel_name": path.parent.name,
            "title": path.stem,
            "started_at": started.isoformat(),
        }
    started = datetime.strptime(
        f"{match['date']} {match['time']}", "%y%m%d %H-%M-%S"
    ).replace(tzinfo=KST)
    return {
        "channel_name": match["channel"],
        "title": match["title"],
        "started_at": started.isoformat(),
    }


async def probe_duration(path: Path) -> float | None:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("Media probe timed out: %s", path)
        return None
    if process.returncode:
        return None
    try:
        value = float(stdout.decode().strip())
    except ValueError:
        return None
    return value if value > 0 else None


async def create_thumbnail(video_path: Path, thumbnail_path: Path, duration: float | None) -> bool:
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    seek = min(10.0, max(0.0, (duration or 0) * 0.1)) if duration and duration < 10 else 10.0
    temp_path = thumbnail_path.with_suffix(".tmp.webp")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(seek),
        "-i", str(video_path), "-frames:v", "1",
        "-vf", "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libwebp", "-quality", "78", str(temp_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        temp_path.unlink(missing_ok=True)
        logger.warning("Thumbnail generation timed out: %s", video_path)
        return False
    if process.returncode == 0 and temp_path.exists() and temp_path.stat().st_size:
        temp_path.replace(thumbnail_path)
        return True
    temp_path.unlink(missing_ok=True)
    return False


def companion_chat_paths(video_path: Path) -> tuple[Path | None, Path | None]:
    chat_dir = video_path.parent / "채팅"
    jsonl = chat_dir / f"{video_path.stem}.jsonl"
    csv_path = chat_dir / f"{video_path.stem}.csv"
    return (jsonl if jsonl.exists() else None, csv_path if csv_path.exists() else None)


class MediaIndexer:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.running = False
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.indexed_count = 0

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scan()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Media indexing failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=config.MEDIA_INDEX_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def scan(self) -> int:
        self.running = True
        seen: set[str] = set()
        count = 0
        try:
            for path in config.FINAL_ROOT.rglob("*.mp4"):
                if not path.is_file() or not within_final_root(path):
                    continue
                resolved = path.resolve()
                seen.add(str(resolved))
                existing = db.query_one("SELECT * FROM media_items WHERE video_path = ?", (str(resolved),))
                session = db.query_one(
                    "SELECT * FROM recording_sessions WHERE final_path = ? ORDER BY id DESC LIMIT 1",
                    (str(resolved),),
                )
                metadata = parse_media_filename(resolved)
                if session:
                    metadata = {
                        "channel_name": str(session.get("channel_name") or metadata["channel_name"]),
                        "title": str(session.get("live_title") or metadata["title"]),
                        "started_at": str(session.get("started_at") or metadata["started_at"]),
                    }
                jsonl, csv_path = companion_chat_paths(resolved)
                if session:
                    session_jsonl = Path(session["chat_jsonl_path"]) if session.get("chat_jsonl_path") else None
                    session_csv = Path(session["chat_csv_path"]) if session.get("chat_csv_path") else None
                    jsonl = session_jsonl if session_jsonl and session_jsonl.exists() else jsonl
                    csv_path = session_csv if session_csv and session_csv.exists() else csv_path
                stat = resolved.stat()
                unchanged = existing and int(existing.get("size_bytes") or 0) == stat.st_size
                duration = existing.get("duration_seconds") if unchanged else await probe_duration(resolved)
                digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:24]
                thumbnail = config.APP_DATA_DIR / "thumbnails" / f"{digest}.webp"
                platform = str(session.get("platform")) if session and session.get("platform") else ("twitcasting" if "트윗캐스트" in resolved.parts else "chzzk")
                db.upsert_media_item(
                    video_path=resolved, channel_name=metadata["channel_name"],
                    title=metadata["title"], started_at=metadata["started_at"],
                    platform=platform, session_id=int(session["id"]) if session else None,
                    chat_jsonl_path=jsonl, chat_csv_path=csv_path,
                    thumbnail_path=thumbnail if thumbnail.exists() else None,
                    duration_seconds=duration, size_bytes=stat.st_size,
                )
                if not thumbnail.exists() and duration is not None:
                    try:
                        if await create_thumbnail(resolved, thumbnail, duration):
                            db.execute(
                                "UPDATE media_items SET thumbnail_path=?, updated_at=? WHERE video_path=?",
                                (str(thumbnail), utc_now_iso(), str(resolved)),
                            )
                    except Exception as exc:
                        logger.warning("Thumbnail generation failed for %s: %s", resolved, exc)
                count += 1
            for row in db.query_all("SELECT id, video_path FROM media_items WHERE status='available'"):
                if row["video_path"] not in seen:
                    db.execute(
                        "UPDATE media_items SET status='unavailable', updated_at=? WHERE id=?",
                        (utc_now_iso(), int(row["id"])),
                    )
            self.indexed_count = count
            self.last_run_at = utc_now_iso()
            self.last_error = None
            logger.info("Media index complete: %s available file(s)", count)
            return count
        finally:
            self.running = False

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running, "last_run_at": self.last_run_at,
            "last_error": self.last_error, "indexed_count": self.indexed_count,
        }


def load_chat_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    jsonl_path = Path(item["chat_jsonl_path"]) if item.get("chat_jsonl_path") else None
    csv_path = Path(item["chat_csv_path"]) if item.get("chat_csv_path") else None
    if jsonl_path and jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(normalize_chat_row(raw))
    elif csv_path and csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows.extend(normalize_chat_row(row) for row in csv.DictReader(handle))
    return rows


def normalize_chat_row(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("offset_seconds")
    try:
        offset: float | None = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        offset = None
    return {
        "type": str(row.get("type") or "chat"),
        "timestamp": str(row.get("timestamp") or ""),
        "offset_seconds": offset,
        "nickname": str(row.get("nickname") or ""),
        "content": str(row.get("content") or ""),
        "sync_state": "synced" if offset is not None else "missing_video",
    }


def rename_media_item(media_id: int, title: str) -> dict[str, Any] | None:
    item = db.get_media_item(media_id)
    if not item:
        return None
    safe_title = sanitize_name(title, "untitled")
    started = datetime.fromisoformat(str(item["started_at"]).replace("Z", "+00:00"))
    base = recording_name(started, str(item["channel_name"]), safe_title, "")
    sources = [
        ("video_path", Path(item["video_path"]), Path(item["video_path"]).parent / f"{base}.mp4"),
        ("chat_jsonl_path", Path(item["chat_jsonl_path"]) if item.get("chat_jsonl_path") else None,
         Path(item["video_path"]).parent / "채팅" / f"{base}.jsonl"),
        ("chat_csv_path", Path(item["chat_csv_path"]) if item.get("chat_csv_path") else None,
         Path(item["video_path"]).parent / "채팅" / f"{base}.csv"),
    ]
    moves = [(key, source, target) for key, source, target in sources if source and source.exists() and source != target]
    if any(target.exists() for _, _, target in moves):
        raise FileExistsError("A recording with that title already exists")
    completed: list[tuple[Path, Path]] = []
    try:
        for _, source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            completed.append((target, source))
    except Exception:
        for current, original in reversed(completed):
            if current.exists():
                current.replace(original)
        raise
    updates = {key: str(target) for key, _, target in moves}
    db.execute(
        """UPDATE media_items SET title=?, video_path=?, chat_jsonl_path=?, chat_csv_path=?, updated_at=? WHERE id=?""",
        (
            safe_title, updates.get("video_path", item["video_path"]),
            updates.get("chat_jsonl_path", item.get("chat_jsonl_path")),
            updates.get("chat_csv_path", item.get("chat_csv_path")), utc_now_iso(), media_id,
        ),
    )
    if item.get("session_id"):
        db.execute(
            "UPDATE recording_sessions SET live_title=?, final_path=?, chat_jsonl_path=?, chat_csv_path=? WHERE id=?",
            (
                safe_title, updates.get("video_path", item["video_path"]),
                updates.get("chat_jsonl_path", item.get("chat_jsonl_path")),
                updates.get("chat_csv_path", item.get("chat_csv_path")), int(item["session_id"]),
            ),
        )
    return db.get_media_item(media_id)
