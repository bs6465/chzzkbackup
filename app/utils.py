from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config

KST = ZoneInfo("Asia/Seoul")
SPECIAL_CHARS_REMOVER = re.compile(r'[\\/:*?"<>|]')
CONTROL_CHARS_REMOVER = re.compile(r"[\x00-\x1f\x7f]")
SAFE_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_FILENAME_BYTES = 255


def now_kst() -> datetime:
    return datetime.now(KST)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def kst_iso(dt: datetime | None = None) -> str:
    return (dt or now_kst()).astimezone(KST).isoformat(timespec="seconds")


def kst_display(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).isoformat(timespec="seconds")


def sanitize_cookie_value(value: Any) -> str:
    text = CONTROL_CHARS_REMOVER.sub("", str(value or ""))
    return text.replace(";", "").strip()


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    value = str(value)
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * max(4, len(value) - 8)}{value[-4:]}"


def sanitize_name(value: Any, fallback: str = "untitled") -> str:
    text = str(value or "").strip()
    text = SPECIAL_CHARS_REMOVER.sub("", text)
    text = CONTROL_CHARS_REMOVER.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text or fallback


def shorten_filename(filename: str) -> str:
    encoded = filename.encode("utf-8")
    if len(encoded) <= MAX_FILENAME_BYTES:
        return filename
    stem, suffix = os.path.splitext(filename)
    digest = hashlib.sha256(encoded).hexdigest()[:8]
    budget = MAX_FILENAME_BYTES - len(suffix.encode("utf-8")) - 9
    shortened = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
    return f"{shortened}_{digest}{suffix}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find available filename for {path}")


def ensure_storage_dirs(streamer_name: str) -> tuple[Path, Path]:
    safe_name = sanitize_name(streamer_name, fallback="unknown")
    video_dir = config.FINAL_ROOT / safe_name
    chat_dir = video_dir / "채팅"
    video_dir.mkdir(parents=True, exist_ok=True)
    chat_dir.mkdir(parents=True, exist_ok=True)
    return video_dir, chat_dir


def disk_status(path: Path) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "warn": usage.free < config.DISK_WARN_BYTES,
    }


def format_bytes(size: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: Any) -> str:
    try:
        value = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
