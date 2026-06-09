from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

CHAT_FILE_RE = re.compile(
    r"^\[(?P<date>\d{6}) (?P<time>\d{2}-\d{2}-\d{2})\] (?P<streamer>.+?) - (?P<title>.+)\.(?P<ext>jsonl|csv)$"
)


@dataclass(frozen=True)
class ParsedChatFile:
    path: Path
    started_at: datetime
    streamer: str
    title: str
    ext: str


@dataclass
class ChatMergeGroup:
    key: tuple[str, ...]
    files: list[ParsedChatFile] = field(default_factory=list)
    session_ids: set[int] = field(default_factory=set)


def parse_chat_filename(path: Path) -> ParsedChatFile | None:
    match = CHAT_FILE_RE.match(path.name)
    if not match:
        return None
    try:
        started_at = datetime.strptime(f"{match['date']} {match['time']}", "%y%m%d %H-%M-%S")
    except ValueError:
        return None
    return ParsedChatFile(
        path=path,
        started_at=started_at,
        streamer=match["streamer"],
        title=match["title"],
        ext=match["ext"],
    )


def _file_sort_key(item: ParsedChatFile) -> tuple[datetime, str]:
    return item.started_at, item.path.name


def _event_sort_key(row: dict[str, Any], fallback: int) -> tuple[str, float, int]:
    timestamp = str(row.get("timestamp") or "")
    try:
        offset = float(row.get("offset_seconds") or 0)
    except (TypeError, ValueError):
        offset = 0.0
    return timestamp, offset, fallback


def _read_jsonl_rows(paths: list[ParsedChatFile]) -> list[dict[str, Any]]:
    rows = []
    index = 0
    for item in sorted(paths, key=_file_sort_key):
        with item.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    row = {"timestamp": "", "offset_seconds": 0, "raw": line.rstrip("\n")}
                row["_merge_index"] = index
                rows.append(row)
                index += 1
    rows.sort(key=lambda row: _event_sort_key(row, int(row.get("_merge_index", 0))))
    for row in rows:
        row.pop("_merge_index", None)
    return rows


def _read_csv_rows(paths: list[ParsedChatFile]) -> tuple[list[str], list[dict[str, str]]]:
    fieldnames: list[str] = ["type", "timestamp", "offset_seconds", "nickname", "content"]
    rows: list[dict[str, str]] = []
    index = 0
    for item in sorted(paths, key=_file_sort_key):
        with item.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                row["_merge_index"] = str(index)
                rows.append(row)
                index += 1
    rows.sort(key=lambda row: _event_sort_key(row, int(row.get("_merge_index", "0"))))
    for row in rows:
        row.pop("_merge_index", None)
    return fieldnames, rows


def _destination_for(group: ChatMergeGroup, ext: str) -> Path:
    earliest = min(group.files, key=_file_sort_key)
    stem = earliest.path.with_suffix("").name
    return earliest.path.parent / f"{stem}.{ext}"


def _merge_jsonl(files: list[ParsedChatFile], destination: Path) -> None:
    rows = _read_jsonl_rows(files)
    temp_path = destination.with_name(f"{destination.name}.merge.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    temp_path.replace(destination)


def _merge_csv(files: list[ParsedChatFile], destination: Path) -> None:
    fieldnames, rows = _read_csv_rows(files)
    temp_path = destination.with_name(f"{destination.name}.merge.tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(destination)


def discover_chat_groups(chat_dir: Path, database: Any | None = None) -> list[ChatMergeGroup]:
    parsed_files = {
        item.path: item
        for item in (parse_chat_filename(path) for path in chat_dir.glob("*"))
        if item is not None and item.path.is_file()
    }
    groups: dict[tuple[str, ...], ChatMergeGroup] = {}
    assigned: set[Path] = set()

    if database is not None:
        rows = database.query_all(
            """
            SELECT id, live_id, channel_name, live_title, started_at, chat_jsonl_path, chat_csv_path
            FROM recording_sessions
            WHERE chat_jsonl_path LIKE ? OR chat_csv_path LIKE ?
            """,
            (f"{chat_dir}%", f"{chat_dir}%"),
        )
        for row in rows:
            live_id = str(row.get("live_id") or "").strip()
            key = ("live", live_id) if live_id else ("session-title", str(row["channel_name"]), str(row["live_title"]), str(row["started_at"])[:10])
            group = groups.setdefault(key, ChatMergeGroup(key))
            group.session_ids.add(int(row["id"]))
            for value in [row.get("chat_jsonl_path"), row.get("chat_csv_path")]:
                if not value:
                    continue
                parsed = parsed_files.get(Path(value))
                if parsed:
                    group.files.append(parsed)
                    assigned.add(parsed.path)

    known_title_groups = {
        (item.streamer, item.title, item.started_at.strftime("%y%m%d")): group
        for group in groups.values()
        for item in group.files
    }
    for item in parsed_files.values():
        if item.path in assigned:
            continue
        title_key = (item.streamer, item.title, item.started_at.strftime("%y%m%d"))
        group = known_title_groups.get(title_key)
        if group is None:
            key = ("file-title", *title_key)
            group = groups.setdefault(key, ChatMergeGroup(key))
            known_title_groups[title_key] = group
        group.files.append(item)

    return [group for group in groups.values() if group.files]


def merge_chat_fragments(chat_dir: Path, *, database: Any | None = None, delete_originals: bool = False) -> dict[str, int]:
    result = {"groups": 0, "jsonl": 0, "csv": 0, "deleted": 0}
    for group in discover_chat_groups(chat_dir, database):
        by_ext = {
            "jsonl": sorted([item for item in group.files if item.ext == "jsonl"], key=_file_sort_key),
            "csv": sorted([item for item in group.files if item.ext == "csv"], key=_file_sort_key),
        }
        if len(by_ext["jsonl"]) <= 1 and len(by_ext["csv"]) <= 1:
            continue
        result["groups"] += 1
        destinations: dict[str, Path] = {}
        if by_ext["jsonl"]:
            destinations["jsonl"] = _destination_for(group, "jsonl")
            _merge_jsonl(by_ext["jsonl"], destinations["jsonl"])
            result["jsonl"] += 1
        if by_ext["csv"]:
            destinations["csv"] = _destination_for(group, "csv")
            _merge_csv(by_ext["csv"], destinations["csv"])
            result["csv"] += 1
        if database is not None and group.session_ids:
            for session_id in group.session_ids:
                database.execute(
                    """
                    UPDATE recording_sessions
                    SET chat_jsonl_path = COALESCE(?, chat_jsonl_path),
                        chat_csv_path = COALESCE(?, chat_csv_path)
                    WHERE id = ?
                    """,
                    (str(destinations.get("jsonl")) if destinations.get("jsonl") else None, str(destinations.get("csv")) if destinations.get("csv") else None, session_id),
                )
        if delete_originals:
            kept = set(destinations.values())
            for item in set(group.files):
                if item.path in kept:
                    continue
                if item.path.exists():
                    item.path.unlink()
                    result["deleted"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge fragmented ChzzkBackup chat files.")
    parser.add_argument("chat_dir", type=Path)
    parser.add_argument("--delete-originals", action="store_true")
    parser.add_argument("--no-db", action="store_true")
    args = parser.parse_args()

    database = None
    if not args.no_db:
        from .db import db

        database = db
    result = merge_chat_fragments(args.chat_dir, database=database, delete_originals=args.delete_originals)
    print(result)


if __name__ == "__main__":
    main()
