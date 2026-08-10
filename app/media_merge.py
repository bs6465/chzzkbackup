from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .chat_csv_migrate import STANDARD_CSV_FIELDS
from .db import Database
from .retention import RetentionService
from .utils import recording_name, unique_path, utc_now_iso


class MediaMergeError(RuntimeError):
    pass


def probe_media(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,profile,width,height,pix_fmt,r_frame_rate,sample_rate,channels,channel_layout,time_base",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    payload["duration"] = float(payload.get("format", {}).get("duration") or 0)
    return payload


def compatible_stream_signature(probe: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            stream.get("codec_type"), stream.get("codec_name"), stream.get("profile"),
            stream.get("width"), stream.get("height"), stream.get("pix_fmt"),
            stream.get("r_frame_rate"), stream.get("sample_rate"),
            stream.get("channels"), stream.get("channel_layout"), stream.get("time_base"),
        )
        for stream in probe.get("streams", [])
    ]


def read_chat_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MediaMergeError(f"잘못된 채팅 JSONL: {path}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def merge_chat_rows(
    first_rows: list[dict[str, Any]],
    second_rows: list[dict[str, Any]],
    first_duration: float,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source_index, rows in enumerate((first_rows, second_rows)):
        for row in rows:
            copied = dict(row)
            value = copied.get("offset_seconds")
            if value not in (None, ""):
                try:
                    offset = float(value)
                except (TypeError, ValueError):
                    copied["offset_seconds"] = None
                else:
                    copied["offset_seconds"] = round(
                        min(offset, first_duration)
                        if source_index == 0
                        else first_duration + offset,
                        3,
                    )
            merged.append(copied)
    merged.sort(
        key=lambda row: (
            float(row["offset_seconds"])
            if row.get("offset_seconds") not in (None, "")
            else float("inf"),
            str(row.get("timestamp") or ""),
        )
    )
    return merged


def write_chat_files(rows: list[dict[str, Any]], jsonl_path: Path, csv_path: Path) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STANDARD_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in STANDARD_CSV_FIELDS})


def shifted_bookmark_values(
    row: dict[str, Any],
    *,
    timeline_delta: float,
    source_shift: float,
    target_shift: float,
    source_duration: float,
) -> tuple[float, float | None]:
    start = min(float(row.get("offset_seconds") or 0) + source_shift, source_duration)
    raw_start = round(start + timeline_delta - target_shift, 6)
    raw_end: float | None = None
    if row.get("end_offset_seconds") is not None:
        end = min(float(row["end_offset_seconds"]) + source_shift, source_duration)
        raw_end = round(end + timeline_delta - target_shift, 6)
    return raw_start, raw_end


def backup_database(database: Database) -> Path:
    backup_dir = database.path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{database.path.stem}-pre-media-merge-{stamp}.sqlite3"
    backup = sqlite3.connect(destination)
    try:
        with database._lock:
            database._conn.backup(backup)
    finally:
        backup.close()
    return destination


def _staged_path(destination: Path, suffix: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return unique_path(destination.with_name(f".{destination.name}.merge{suffix}"))


def _run_concat(source_paths: list[Path], destination: Path) -> None:
    manifest = _staged_path(destination, ".ffconcat")
    try:
        lines = ["ffconcat version 1.0"]
        for path in source_paths:
            escaped = str(path).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(manifest),
                "-map", "0", "-c", "copy", "-movflags", "+faststart",
                "-f", "mp4", str(destination),
            ],
            check=True,
        )
    finally:
        manifest.unlink(missing_ok=True)


def _validate_decode(path: Path, boundary: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(max(0, boundary - 2)), "-i", str(path),
            "-t", "5", "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def merge_media_items(
    database: Database,
    media_ids: list[int],
    title: str,
    *,
    execute: bool,
) -> dict[str, Any]:
    if len(media_ids) != 2 or len(set(media_ids)) != 2:
        raise MediaMergeError("서로 다른 원본 미디어 ID 2개가 필요합니다.")
    items = [database.get_media_item(media_id) for media_id in media_ids]
    if any(not item or item.get("status") != "available" for item in items):
        raise MediaMergeError("원본 미디어가 없거나 보관함에서 사용할 수 없습니다.")
    first, second = items  # type: ignore[misc]
    if str(first["channel_name"]) != str(second["channel_name"]):
        raise MediaMergeError("서로 다른 채널의 영상은 합칠 수 없습니다.")
    source_paths = [Path(str(first["video_path"])), Path(str(second["video_path"]))]
    if any(not path.is_file() for path in source_paths):
        raise MediaMergeError("원본 영상 파일을 찾을 수 없습니다.")
    chat_paths = [Path(str(item["chat_jsonl_path"])) for item in (first, second)]
    if any(not path.is_file() for path in chat_paths):
        raise MediaMergeError("원본 채팅 JSONL 파일을 찾을 수 없습니다.")

    probes = [probe_media(path) for path in source_paths]
    if compatible_stream_signature(probes[0]) != compatible_stream_signature(probes[1]):
        raise MediaMergeError("두 영상의 스트림 규격이 달라 무인코딩 병합할 수 없습니다.")
    first_duration = float(probes[0]["duration"])
    total_duration = sum(float(probe["duration"]) for probe in probes)
    first_session = database.get_session(int(first["session_id"])) if first.get("session_id") else None
    second_session = database.get_session(int(second["session_id"])) if second.get("session_id") else None
    if first_session and second_session:
        first_live_id = first_session.get("live_id")
        second_live_id = second_session.get("live_id")
        if first_live_id and second_live_id and first_live_id != second_live_id:
            raise MediaMergeError("서로 다른 생방송 ID의 영상입니다.")

    started_at = datetime.fromisoformat(str(first["started_at"]).replace("Z", "+00:00"))
    base = recording_name(started_at, str(first["channel_name"]), title, "")
    video_target = source_paths[0].parent / f"{base}.mp4"
    chat_dir = source_paths[0].parent / "채팅"
    jsonl_target = chat_dir / f"{base}.jsonl"
    csv_target = chat_dir / f"{base}.csv"
    targets = [video_target, jsonl_target, csv_target]
    if any(path.exists() for path in targets):
        raise MediaMergeError("병합 결과 파일명이 이미 존재합니다.")

    preview = {
        "source_media_ids": media_ids,
        "first_duration": first_duration,
        "expected_duration": total_duration,
        "video_path": str(video_target),
        "title": title,
    }
    if not execute:
        return preview

    backup_path = backup_database(database)
    staged_video = _staged_path(video_target, ".mp4.part")
    staged_jsonl = _staged_path(jsonl_target, ".jsonl.part")
    staged_csv = _staged_path(csv_target, ".csv.part")
    merged_id: int | None = None
    trashed: list[int] = []
    session_original = dict(first_session) if first_session else None
    try:
        _run_concat(source_paths, staged_video)
        merged_probe = probe_media(staged_video)
        if abs(float(merged_probe["duration"]) - total_duration) > 1.0:
            raise MediaMergeError("병합 영상 길이가 원본 합계와 맞지 않습니다.")
        if compatible_stream_signature(merged_probe) != compatible_stream_signature(probes[0]):
            raise MediaMergeError("병합 영상 스트림 검증에 실패했습니다.")
        _validate_decode(staged_video, first_duration)

        first_rows = read_chat_rows(chat_paths[0])
        second_rows = read_chat_rows(chat_paths[1])
        merged_rows = merge_chat_rows(first_rows, second_rows, first_duration)
        if len(merged_rows) != len(first_rows) + len(second_rows):
            raise MediaMergeError("병합 채팅 개수가 원본 합계와 맞지 않습니다.")
        write_chat_files(merged_rows, staged_jsonl, staged_csv)

        staged_video.replace(video_target)
        staged_jsonl.replace(jsonl_target)
        staged_csv.replace(csv_target)
        merged_id = database.upsert_media_item(
            video_path=video_target,
            channel_name=str(first["channel_name"]),
            title=title.strip(),
            started_at=str(first["started_at"]),
            platform=str(first.get("platform") or "chzzk"),
            session_id=int(first["session_id"]) if first.get("session_id") else None,
            channel_id=str(first["channel_id"]) if first.get("channel_id") else None,
            chat_jsonl_path=jsonl_target,
            chat_csv_path=csv_target,
            duration_seconds=float(merged_probe["duration"]),
            size_bytes=video_target.stat().st_size,
        )
        merged_media = database.get_media_item(merged_id) or {}
        target_shift = float(merged_media.get("bookmark_shift_seconds") or 0)
        timeline_delta = 0.0
        for source, probe in zip((first, second), probes, strict=True):
            source_shift = float(source.get("bookmark_shift_seconds") or 0)
            for row in database.query_all(
                "SELECT * FROM video_bookmarks WHERE media_id=? ORDER BY id",
                (int(source["id"]),),
            ):
                start, end = shifted_bookmark_values(
                    row,
                    timeline_delta=timeline_delta,
                    source_shift=source_shift,
                    target_shift=target_shift,
                    source_duration=float(probe["duration"]),
                )
                now = utc_now_iso()
                database.execute(
                    """
                    INSERT INTO video_bookmarks
                      (session_id, media_id, kind, marked_at, offset_seconds,
                       end_marked_at, end_offset_seconds, content, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(first["session_id"]) if first.get("session_id") else None,
                        merged_id,
                        str(row.get("kind") or "point"),
                        row.get("marked_at"),
                        start,
                        row.get("end_marked_at"),
                        end,
                        str(row.get("content") or ""),
                        row.get("created_at") or now,
                        now,
                    ),
                )
            timeline_delta += float(probe["duration"])

        if first_session:
            database.execute(
                """
                UPDATE recording_sessions
                SET live_title=?, final_path=?, chat_jsonl_path=?, chat_csv_path=?
                WHERE id=?
                """,
                (title.strip(), str(video_target), str(jsonl_target), str(csv_target), int(first_session["id"])),
            )

        retention = RetentionService(database)
        for source in (first, second):
            media_id = int(source["id"])
            if not retention.trash_media(media_id, "0.4.5 병합 원본"):
                raise MediaMergeError(f"원본 미디어 {media_id}을 휴지통으로 옮기지 못했습니다.")
            trashed.append(media_id)

        result = {
            **preview,
            "merged_media_id": merged_id,
            "duration": float(merged_probe["duration"]),
            "chat_count": len(merged_rows),
            "bookmark_count": int(
                database.query_one(
                    "SELECT count(*) AS count FROM video_bookmarks WHERE media_id=?",
                    (merged_id,),
                )["count"]
            ),
            "trashed_media_ids": trashed,
            "database_backup": str(backup_path),
        }
        return result
    except Exception:
        retention = RetentionService(database)
        for media_id in reversed(trashed):
            try:
                retention.restore_media(media_id)
            except Exception:
                pass
        if merged_id is not None:
            database.execute("DELETE FROM media_items WHERE id=?", (merged_id,))
        if session_original:
            database.execute(
                """
                UPDATE recording_sessions
                SET live_title=?, final_path=?, chat_jsonl_path=?, chat_csv_path=?
                WHERE id=?
                """,
                (
                    session_original.get("live_title"), session_original.get("final_path"),
                    session_original.get("chat_jsonl_path"), session_original.get("chat_csv_path"),
                    int(session_original["id"]),
                ),
            )
        for path in [*targets, staged_video, staged_jsonl, staged_csv]:
            path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="두 ChzzkBackup 미디어를 안전하게 병합합니다.")
    parser.add_argument("media_ids", nargs=2, type=int)
    parser.add_argument("--title", required=True)
    parser.add_argument("--execute", action="store_true", help="검증 후 실제 파일과 DB를 변경합니다.")
    args = parser.parse_args()
    from .db import db

    result = merge_media_items(db, args.media_ids, args.title, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
