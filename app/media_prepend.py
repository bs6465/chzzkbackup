from __future__ import annotations

import argparse
import csv
import difflib
import json
import statistics
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .db import Database
from .media_merge import (
    MediaMergeError,
    _run_concat,
    _staged_path,
    _validate_decode,
    backup_database,
    compatible_stream_signature,
    merge_chat_rows,
    probe_media,
    read_chat_rows,
    shifted_bookmark_values,
    write_chat_files,
)
from .retention import RetentionService
from .utils import recording_name, unique_path, utc_now_iso


def parse_playback_seconds(value: Any) -> float:
    parts = str(value or "").strip().split(":")
    if len(parts) not in (2, 3):
        raise MediaMergeError(f"잘못된 전체 채팅 재생시간: {value}")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise MediaMergeError(f"잘못된 전체 채팅 재생시간: {value}") from exc
    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0.0
    else:
        hours, minutes, seconds = numbers
    if min(hours, minutes, seconds) < 0 or minutes >= 60 or seconds >= 60:
        raise MediaMergeError(f"잘못된 전체 채팅 재생시간: {value}")
    return hours * 3600 + minutes * 60 + seconds


def read_external_chat_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"재생시간", "닉네임", "id", "메시지"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise MediaMergeError("전체 채팅 CSV 열 형식이 올바르지 않습니다.")
        return [
            {
                "playback_seconds": parse_playback_seconds(row["재생시간"]),
                "nickname": str(row["닉네임"] or ""),
                "user_id": str(row["id"] or ""),
                "content": str(row["메시지"] or ""),
            }
            for row in reader
        ]


def infer_target_timeline_start(
    external_rows: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
) -> tuple[float, int]:
    external_keys = [
        (str(row.get("nickname") or ""), str(row.get("content") or ""))
        for row in external_rows
    ]
    existing_keys = [
        (str(row.get("nickname") or ""), str(row.get("content") or ""))
        for row in existing_rows
    ]
    matcher = difflib.SequenceMatcher(
        None,
        external_keys,
        existing_keys,
        autojunk=False,
    )
    deltas: list[float] = []
    for block in matcher.get_matching_blocks():
        if block.size < 3:
            continue
        for index in range(block.size):
            external = external_rows[block.a + index]
            existing = existing_rows[block.b + index]
            offset = existing.get("offset_seconds")
            if offset in (None, ""):
                continue
            try:
                deltas.append(
                    float(external["playback_seconds"]) - float(offset)
                )
            except (TypeError, ValueError):
                continue
    if len(deltas) < 20:
        raise MediaMergeError("전체 CSV와 기존 채팅의 연결 지점을 찾지 못했습니다.")
    center = statistics.median(deltas)
    inliers = [delta for delta in deltas if abs(delta - center) <= 3.0]
    if len(inliers) < 20:
        raise MediaMergeError("전체 CSV와 기존 채팅의 시간축이 일치하지 않습니다.")
    return round(statistics.median(inliers), 3), len(inliers)


def build_prefix_chat_rows(
    external_rows: list[dict[str, Any]],
    *,
    prefix_timeline_start: float,
    target_timeline_start: float,
    merged_started_at: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in external_rows:
        playback = float(source["playback_seconds"])
        if not prefix_timeline_start <= playback < target_timeline_start:
            continue
        offset = round(playback - prefix_timeline_start, 3)
        content = str(source.get("content") or "")
        rows.append(
            {
                "type": "donation" if content.startswith("[후원 ") else "chat",
                "timestamp": (merged_started_at + timedelta(seconds=offset)).isoformat(),
                "offset_seconds": offset,
                "nickname": str(source.get("nickname") or ""),
                "content": content,
                "raw": {
                    "user_id_hash": str(source.get("user_id") or ""),
                    "source": "external_chat_csv",
                },
            }
        )
    return rows


def _stream(probe: dict[str, Any], kind: str) -> dict[str, Any]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == kind:
            return stream
    raise MediaMergeError(f"{kind} 스트림을 찾을 수 없습니다.")


def transcode_to_reference(source: Path, reference: dict[str, Any], output: Path) -> None:
    video = _stream(reference, "video")
    audio = _stream(reference, "audio")
    frame_rate = str(video.get("r_frame_rate") or "")
    time_base = str(video.get("time_base") or "")
    try:
        time_base_numerator, time_base_denominator = [
            int(part) for part in time_base.split("/", 1)
        ]
    except (TypeError, ValueError) as exc:
        raise MediaMergeError("기존 영상의 비디오 시간축을 읽지 못했습니다.") from exc
    if time_base_numerator != 1 or time_base_denominator <= 0:
        raise MediaMergeError("지원하지 않는 기존 영상 비디오 시간축입니다.")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", f"fps={frame_rate}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-profile:v", str(video.get("profile") or "high").lower(),
            "-pix_fmt", str(video.get("pix_fmt") or "yuv420p"),
            "-fps_mode", "cfr",
            "-video_track_timescale", str(time_base_denominator),
            "-c:a", "aac", "-b:a", "128k",
            "-ar", str(audio.get("sample_rate") or "48000"),
            "-ac", str(audio.get("channels") or "2"),
            "-movflags", "+faststart",
            str(output),
        ],
        check=True,
    )


def prepend_external_media(
    database: Database,
    target_media_id: int,
    video_path: Path,
    full_chat_csv_path: Path,
    *,
    execute: bool,
) -> dict[str, Any]:
    target = database.get_media_item(target_media_id)
    if not target or target.get("status") != "available":
        raise MediaMergeError("대상 미디어가 없거나 보관함에서 사용할 수 없습니다.")
    target_video = Path(str(target["video_path"]))
    target_chat = Path(str(target.get("chat_jsonl_path") or ""))
    if not video_path.is_file() or not full_chat_csv_path.is_file():
        raise MediaMergeError("첨부 영상 또는 전체 채팅 CSV를 찾을 수 없습니다.")
    if not target_video.is_file() or not target_chat.is_file():
        raise MediaMergeError("대상 영상 또는 기존 채팅 JSONL을 찾을 수 없습니다.")

    source_probe = probe_media(video_path)
    target_probe = probe_media(target_video)
    external_rows = read_external_chat_csv(full_chat_csv_path)
    existing_rows = read_chat_rows(target_chat)
    target_timeline_start, matched_chat_count = infer_target_timeline_start(
        external_rows,
        existing_rows,
    )
    source_duration = float(source_probe["duration"])
    prefix_timeline_start = target_timeline_start - source_duration
    if prefix_timeline_start < 0:
        raise MediaMergeError("첨부 영상 길이가 계산된 앞부분 구간보다 깁니다.")
    needs_transcode = (
        compatible_stream_signature(source_probe)
        != compatible_stream_signature(target_probe)
    )
    preview = {
        "target_media_id": target_media_id,
        "title": str(target["title"]),
        "source_duration": source_duration,
        "target_duration": float(target_probe["duration"]),
        "expected_duration": source_duration + float(target_probe["duration"]),
        "target_timeline_start": target_timeline_start,
        "prefix_timeline_start": round(prefix_timeline_start, 3),
        "matched_chat_count": matched_chat_count,
        "needs_transcode": needs_transcode,
    }
    if not execute:
        return preview

    backup_path = backup_database(database)
    normalized_path: Path | None = None
    staged_video: Path | None = None
    staged_jsonl: Path | None = None
    staged_csv: Path | None = None
    targets: list[Path] = []
    merged_id: int | None = None
    trashed = False
    session = (
        database.get_session(int(target["session_id"]))
        if target.get("session_id")
        else None
    )
    session_original = dict(session) if session else None
    try:
        prepared_video = video_path
        if needs_transcode:
            config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
            normalized_path = unique_path(
                config.TEMP_DIR / f"prepend-{target_media_id}.normalized.mp4"
            )
            transcode_to_reference(video_path, target_probe, normalized_path)
            prepared_video = normalized_path
        prepared_probe = probe_media(prepared_video)
        if compatible_stream_signature(prepared_probe) != compatible_stream_signature(
            target_probe
        ):
            raise MediaMergeError("첨부 영상을 기존 영상 규격으로 맞추지 못했습니다.")
        prepared_duration = float(prepared_probe["duration"])
        if abs(prepared_duration - source_duration) > 1.0:
            raise MediaMergeError("변환된 첨부 영상 길이가 원본과 맞지 않습니다.")

        prefix_timeline_start = target_timeline_start - prepared_duration
        target_started_at = datetime.fromisoformat(
            str(target["started_at"]).replace("Z", "+00:00")
        )
        merged_started_at = target_started_at - timedelta(seconds=prepared_duration)
        base = recording_name(
            merged_started_at,
            str(target["channel_name"]),
            str(target["title"]),
            "",
        )
        video_target = target_video.parent / f"{base}.mp4"
        chat_dir = target_video.parent / "채팅"
        jsonl_target = chat_dir / f"{base}.jsonl"
        csv_target = chat_dir / f"{base}.csv"
        targets = [video_target, jsonl_target, csv_target]
        if any(path.exists() for path in targets):
            raise MediaMergeError("병합 결과 파일명이 이미 존재합니다.")

        staged_video = _staged_path(video_target, ".mp4.part")
        staged_jsonl = _staged_path(jsonl_target, ".jsonl.part")
        staged_csv = _staged_path(csv_target, ".csv.part")
        _run_concat([prepared_video, target_video], staged_video)
        merged_probe = probe_media(staged_video)
        expected_duration = prepared_duration + float(target_probe["duration"])
        if abs(float(merged_probe["duration"]) - expected_duration) > 1.0:
            raise MediaMergeError("병합 영상 길이가 원본 합계와 맞지 않습니다.")
        if compatible_stream_signature(merged_probe) != compatible_stream_signature(
            target_probe
        ):
            raise MediaMergeError("병합 영상 스트림 검증에 실패했습니다.")
        _validate_decode(staged_video, prepared_duration)

        prefix_rows = build_prefix_chat_rows(
            external_rows,
            prefix_timeline_start=prefix_timeline_start,
            target_timeline_start=target_timeline_start,
            merged_started_at=merged_started_at,
        )
        merged_rows = merge_chat_rows(prefix_rows, existing_rows, prepared_duration)
        if len(merged_rows) != len(prefix_rows) + len(existing_rows):
            raise MediaMergeError("병합 채팅 개수가 원본 합계와 맞지 않습니다.")
        write_chat_files(merged_rows, staged_jsonl, staged_csv)

        staged_video.replace(video_target)
        staged_jsonl.replace(jsonl_target)
        staged_csv.replace(csv_target)
        merged_id = database.upsert_media_item(
            video_path=video_target,
            channel_name=str(target["channel_name"]),
            title=str(target["title"]),
            started_at=merged_started_at.isoformat(),
            platform=str(target.get("platform") or "chzzk"),
            session_id=int(target["session_id"]) if target.get("session_id") else None,
            channel_id=str(target["channel_id"]) if target.get("channel_id") else None,
            chat_jsonl_path=jsonl_target,
            chat_csv_path=csv_target,
            thumbnail_path=(
                Path(str(target["thumbnail_path"]))
                if target.get("thumbnail_path")
                else None
            ),
            duration_seconds=float(merged_probe["duration"]),
            size_bytes=video_target.stat().st_size,
            retention_policy_key=(
                str(target["retention_policy_key"])
                if target.get("retention_policy_key")
                else None
            ),
        )
        database.execute(
            """
            UPDATE media_items
            SET chat_delay_seconds=?, bookmark_shift_seconds=?,
                retention_override=?, retention_expires_at=?
            WHERE id=?
            """,
            (
                float(target.get("chat_delay_seconds") or 0),
                float(target.get("bookmark_shift_seconds") or 0),
                str(target.get("retention_override") or "inherit"),
                target.get("retention_expires_at"),
                merged_id,
            ),
        )
        target_shift = float(target.get("bookmark_shift_seconds") or 0)
        for row in database.query_all(
            "SELECT * FROM video_bookmarks WHERE media_id=? ORDER BY id",
            (target_media_id,),
        ):
            start, end = shifted_bookmark_values(
                row,
                timeline_delta=prepared_duration,
                source_shift=target_shift,
                target_shift=target_shift,
                source_duration=float(target_probe["duration"]),
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
                    int(target["session_id"]) if target.get("session_id") else None,
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

        if session:
            database.execute(
                """
                UPDATE recording_sessions
                SET live_title=?, started_at=?, final_path=?,
                    chat_jsonl_path=?, chat_csv_path=?
                WHERE id=?
                """,
                (
                    str(target["title"]),
                    merged_started_at.isoformat(),
                    str(video_target),
                    str(jsonl_target),
                    str(csv_target),
                    int(session["id"]),
                ),
            )

        retention = RetentionService(database)
        if not retention.trash_media(target_media_id, "0.4.7 외부 앞부분 병합 원본"):
            raise MediaMergeError("기존 미디어를 휴지통으로 옮기지 못했습니다.")
        trashed = True
        return {
            **preview,
            "prepared_duration": prepared_duration,
            "duration": float(merged_probe["duration"]),
            "prefix_chat_count": len(prefix_rows),
            "chat_count": len(merged_rows),
            "bookmark_count": int(
                database.query_one(
                    "SELECT count(*) AS count FROM video_bookmarks WHERE media_id=?",
                    (merged_id,),
                )["count"]
            ),
            "merged_media_id": merged_id,
            "trashed_media_id": target_media_id,
            "video_path": str(video_target),
            "database_backup": str(backup_path),
        }
    except Exception:
        retention = RetentionService(database)
        if trashed:
            try:
                retention.restore_media(target_media_id)
            except Exception:
                pass
        if merged_id is not None:
            database.execute("DELETE FROM media_items WHERE id=?", (merged_id,))
        if session_original:
            database.execute(
                """
                UPDATE recording_sessions
                SET live_title=?, started_at=?, final_path=?,
                    chat_jsonl_path=?, chat_csv_path=?
                WHERE id=?
                """,
                (
                    session_original.get("live_title"),
                    session_original.get("started_at"),
                    session_original.get("final_path"),
                    session_original.get("chat_jsonl_path"),
                    session_original.get("chat_csv_path"),
                    int(session_original["id"]),
                ),
            )
        for path in [*targets, staged_video, staged_jsonl, staged_csv]:
            if path is not None:
                path.unlink(missing_ok=True)
        raise
    finally:
        if normalized_path is not None:
            normalized_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="외부 앞부분 영상과 전체 채팅 CSV를 기존 미디어 앞에 병합합니다."
    )
    parser.add_argument("target_media_id", type=int)
    parser.add_argument("video_path", type=Path)
    parser.add_argument("full_chat_csv_path", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="검증 후 실제 파일과 DB를 변경합니다.",
    )
    args = parser.parse_args()
    from .db import db

    result = prepend_external_media(
        db,
        args.target_media_id,
        args.video_path,
        args.full_chat_csv_path,
        execute=args.execute,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
