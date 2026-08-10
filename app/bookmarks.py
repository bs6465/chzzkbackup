from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .db import Database
from .utils import utc_now_iso


BOOKMARK_CONTENT_MAX_LENGTH = 500
BOOKMARK_SHIFT_MIN = -60.0
BOOKMARK_SHIFT_MAX = 60.0
BOOKMARK_SHIFT_STEP = 0.5
BOOKMARK_KINDS = {"point", "range"}
ACTIVE_RECORDING_STATUSES = {"recording", "retrying"}


class BookmarkNotFoundError(KeyError):
    pass


class BookmarkValidationError(ValueError):
    pass


class InactiveRecordingError(RuntimeError):
    pass


def normalize_bookmark_content(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise BookmarkValidationError("북마크 내용은 한 줄로 입력해 주세요.")
    content = value.strip()
    if len(content) > BOOKMARK_CONTENT_MAX_LENGTH:
        raise BookmarkValidationError(
            f"북마크 내용은 {BOOKMARK_CONTENT_MAX_LENGTH}자까지 입력할 수 있습니다."
        )
    return content


def normalize_sync_seconds(value: Any, label: str = "시간 보정값") -> float:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BookmarkValidationError(f"{label}이 올바르지 않습니다.") from exc
    if not parsed.is_finite():
        raise BookmarkValidationError(f"{label}이 올바르지 않습니다.")
    steps = (parsed / Decimal(str(BOOKMARK_SHIFT_STEP))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    normalized = steps * Decimal(str(BOOKMARK_SHIFT_STEP))
    normalized = max(Decimal(str(BOOKMARK_SHIFT_MIN)), normalized)
    normalized = min(Decimal(str(BOOKMARK_SHIFT_MAX)), normalized)
    return float(normalized)


def normalize_bookmark_shift(value: Any) -> float:
    return normalize_sync_seconds(value, "북마크 보정값")


def normalize_bookmark_kind(value: Any) -> str:
    kind = str(value or "point").strip().lower()
    if kind not in BOOKMARK_KINDS:
        raise BookmarkValidationError("북마크 종류가 올바르지 않습니다.")
    return kind


def validate_offset_seconds(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BookmarkValidationError("북마크 시간이 올바르지 않습니다.") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise BookmarkValidationError("북마크 시간은 0초 이상이어야 합니다.")
    return parsed


def effective_bookmark_offset(
    offset_seconds: float,
    shift_seconds: float,
    duration_seconds: float | None = None,
) -> float:
    value = max(0.0, float(offset_seconds) + float(shift_seconds))
    if duration_seconds is not None and math.isfinite(duration_seconds):
        value = min(value, max(0.0, duration_seconds))
    return round(value, 3)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class BookmarkService:
    def __init__(self, database: Database) -> None:
        self.db = database

    def _session_or_raise(self, session_id: int) -> dict[str, Any]:
        session = self.db.get_session(session_id)
        if not session:
            raise BookmarkNotFoundError("녹화 세션을 찾을 수 없습니다.")
        return session

    def _media_or_raise(self, media_id: int) -> dict[str, Any]:
        media = self.db.get_media_item(media_id)
        if not media:
            raise BookmarkNotFoundError("영상을 찾을 수 없습니다.")
        return media

    def _bookmark_or_raise(self, bookmark_id: int) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM video_bookmarks WHERE id = ?", (bookmark_id,)
        )
        if not row:
            raise BookmarkNotFoundError("북마크를 찾을 수 없습니다.")
        return row

    def _segment_windows(
        self, session_id: int
    ) -> tuple[list[tuple[datetime, datetime, float, float]], float]:
        windows: list[tuple[datetime, datetime, float, float]] = []
        elapsed = 0.0
        for segment in self.db.recording_segments(session_id):
            if not segment.get("has_video"):
                continue
            started_at = _parse_datetime(segment.get("started_at"))
            if not started_at:
                continue
            try:
                duration = float(segment.get("duration_seconds") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if not math.isfinite(duration) or duration <= 0:
                ended_at = _parse_datetime(segment.get("ended_at"))
                duration = max(
                    0.0,
                    (ended_at - started_at).total_seconds() if ended_at else 0.0,
                )
            end_offset = elapsed + duration
            windows.append(
                (started_at, started_at + timedelta(seconds=duration), elapsed, end_offset)
            )
            elapsed = end_offset
        return windows, elapsed

    def map_live_timestamp(
        self,
        session_id: int,
        marked_at: str,
        *,
        include_current_segment: bool,
    ) -> float:
        session = self._session_or_raise(session_id)
        marked = _parse_datetime(marked_at)
        if not marked:
            return 0.0
        windows, elapsed = self._segment_windows(session_id)
        current_started = _parse_datetime(session.get("current_segment_started_at"))
        if (
            include_current_segment
            and current_started
            and str(session.get("status")) in ACTIVE_RECORDING_STATUSES
            and marked >= current_started
        ):
            return round(elapsed + (marked - current_started).total_seconds(), 3)

        boundaries: list[tuple[float, float]] = []
        for started, ended, start_offset, end_offset in windows:
            if started <= marked <= ended:
                return round(
                    min(end_offset, start_offset + (marked - started).total_seconds()), 3
                )
            boundaries.append((abs((marked - started).total_seconds()), start_offset))
            boundaries.append((abs((marked - ended).total_seconds()), end_offset))
        if current_started:
            boundaries.append((abs((marked - current_started).total_seconds()), elapsed))
        if boundaries:
            return round(min(boundaries, key=lambda item: item[0])[1], 3)
        session_started = _parse_datetime(session.get("started_at"))
        if include_current_segment and session_started and marked >= session_started:
            return round((marked - session_started).total_seconds(), 3)
        return 0.0

    def attach_session_bookmarks(self, media_id: int) -> None:
        media = self._media_or_raise(media_id)
        session_id = media.get("session_id")
        if session_id is None:
            return
        rows = self.db.query_all(
            "SELECT * FROM video_bookmarks WHERE session_id = ? ORDER BY id",
            (int(session_id),),
        )
        for row in rows:
            updates: list[str] = []
            params: list[Any] = []
            if row.get("media_id") is None:
                updates.append("media_id = ?")
                params.append(media_id)
            if row.get("offset_seconds") is None:
                updates.append("offset_seconds = ?")
                params.append(
                    self.map_live_timestamp(
                        int(session_id),
                        str(row.get("marked_at") or media.get("started_at") or ""),
                        include_current_segment=False,
                    )
                )
            if (
                normalize_bookmark_kind(row.get("kind")) == "range"
                and row.get("end_marked_at")
                and row.get("end_offset_seconds") is None
            ):
                updates.append("end_offset_seconds = ?")
                params.append(
                    self.map_live_timestamp(
                        int(session_id),
                        str(row["end_marked_at"]),
                        include_current_segment=False,
                    )
                )
            if updates:
                updates.append("updated_at = ?")
                params.extend([utc_now_iso(), int(row["id"])])
                self.db.execute(
                    f"UPDATE video_bookmarks SET {', '.join(updates)} WHERE id = ?",
                    tuple(params),
                )
                self._normalize_range_order(int(row["id"]), include_current=False)

    def _duration(self, media: dict[str, Any]) -> float | None:
        try:
            duration = float(media.get("duration_seconds"))
        except (TypeError, ValueError):
            return None
        return duration if math.isfinite(duration) and duration >= 0 else None

    def _channel_defaults(self, media: dict[str, Any]) -> tuple[float, float]:
        channel_id = media.get("channel_id")
        if not channel_id and media.get("session_id") is not None:
            session = self.db.get_session(int(media["session_id"])) or {}
            channel_id = session.get("channel_id")
        channel = self.db.get_channel(str(channel_id)) if channel_id else None
        return (
            normalize_sync_seconds(
                channel.get("default_chat_delay_seconds") or 0 if channel else 0,
                "채팅 보정값",
            ),
            normalize_bookmark_shift(
                channel.get("default_bookmark_shift_seconds") or 0 if channel else 0
            ),
        )

    def media_sync(self, media_id: int) -> dict[str, Any]:
        media = self._media_or_raise(media_id)
        default_chat, default_bookmark = self._channel_defaults(media)
        return {
            "media_id": media_id,
            "chat_delay_seconds": normalize_sync_seconds(
                media.get("chat_delay_seconds") or 0, "채팅 보정값"
            ),
            "bookmark_shift_seconds": normalize_bookmark_shift(
                media.get("bookmark_shift_seconds") or 0
            ),
            "channel_default_chat_delay_seconds": default_chat,
            "channel_default_bookmark_shift_seconds": default_bookmark,
            "minimum": BOOKMARK_SHIFT_MIN,
            "maximum": BOOKMARK_SHIFT_MAX,
            "step": BOOKMARK_SHIFT_STEP,
        }

    def _row_offsets(
        self,
        row: dict[str, Any],
        *,
        include_current: bool,
    ) -> tuple[float, float | None, bool, bool]:
        session_id = int(row["session_id"]) if row.get("session_id") is not None else None
        start_resolved = row.get("offset_seconds") is not None
        start = (
            float(row["offset_seconds"])
            if start_resolved
            else self.map_live_timestamp(
                int(session_id), str(row.get("marked_at") or ""),
                include_current_segment=include_current,
            )
        )
        kind = normalize_bookmark_kind(row.get("kind"))
        end_complete = kind == "range" and (
            row.get("end_offset_seconds") is not None or bool(row.get("end_marked_at"))
        )
        end_resolved = end_complete and row.get("end_offset_seconds") is not None
        end: float | None = None
        if end_complete:
            end = (
                float(row["end_offset_seconds"])
                if end_resolved
                else self.map_live_timestamp(
                    int(session_id), str(row.get("end_marked_at") or ""),
                    include_current_segment=include_current,
                )
            )
        return start, end, start_resolved, end_resolved

    def _payload(
        self,
        row: dict[str, Any],
        *,
        raw_offset: float,
        raw_end_offset: float | None,
        shift_seconds: float,
        duration_seconds: float | None,
        resolved: bool,
        end_resolved: bool,
    ) -> dict[str, Any]:
        kind = normalize_bookmark_kind(row.get("kind"))
        return {
            "id": int(row["id"]),
            "session_id": int(row["session_id"]) if row.get("session_id") is not None else None,
            "media_id": int(row["media_id"]) if row.get("media_id") is not None else None,
            "kind": kind,
            "offset_seconds": round(float(raw_offset), 3),
            "effective_offset_seconds": effective_bookmark_offset(
                raw_offset, shift_seconds, duration_seconds
            ),
            "end_offset_seconds": (
                round(float(raw_end_offset), 3) if raw_end_offset is not None else None
            ),
            "effective_end_offset_seconds": (
                effective_bookmark_offset(raw_end_offset, shift_seconds, duration_seconds)
                if raw_end_offset is not None else None
            ),
            "content": str(row.get("content") or ""),
            "marked_at": row.get("marked_at"),
            "end_marked_at": row.get("end_marked_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "resolved": resolved,
            "end_resolved": end_resolved,
            "complete": kind == "point" or raw_end_offset is not None,
        }

    def live_collection(self, session_id: int) -> dict[str, Any]:
        session = self._session_or_raise(session_id)
        bookmarks = []
        for row in self.db.query_all(
            "SELECT * FROM video_bookmarks WHERE session_id = ? ORDER BY id",
            (session_id,),
        ):
            start, end, resolved, end_resolved = self._row_offsets(
                row, include_current=True
            )
            bookmarks.append(
                self._payload(
                    row,
                    raw_offset=start,
                    raw_end_offset=end,
                    shift_seconds=0,
                    duration_seconds=None,
                    resolved=resolved,
                    end_resolved=end_resolved,
                )
            )
        bookmarks.sort(key=lambda item: (item["effective_offset_seconds"], item["id"]))
        return {
            "scope": "recording",
            "session_id": session_id,
            "status": str(session.get("status") or ""),
            "shift_seconds": 0.0,
            "bookmarks": bookmarks,
        }

    def media_collection(self, media_id: int) -> dict[str, Any]:
        self.attach_session_bookmarks(media_id)
        media = self._media_or_raise(media_id)
        shift = normalize_bookmark_shift(media.get("bookmark_shift_seconds") or 0)
        duration = self._duration(media)
        bookmarks = []
        for row in self.db.query_all(
            "SELECT * FROM video_bookmarks WHERE media_id = ? ORDER BY id", (media_id,)
        ):
            start, end, _, _ = self._row_offsets(row, include_current=False)
            bookmarks.append(
                self._payload(
                    row,
                    raw_offset=start,
                    raw_end_offset=end,
                    shift_seconds=shift,
                    duration_seconds=duration,
                    resolved=True,
                    end_resolved=end is not None,
                )
            )
        bookmarks.sort(key=lambda item: (item["effective_offset_seconds"], item["id"]))
        sync = self.media_sync(media_id)
        return {
            "scope": "media",
            "media_id": media_id,
            "shift_seconds": shift,
            "channel_default_shift_seconds": sync[
                "channel_default_bookmark_shift_seconds"
            ],
            "shift_min": BOOKMARK_SHIFT_MIN,
            "shift_max": BOOKMARK_SHIFT_MAX,
            "shift_step": BOOKMARK_SHIFT_STEP,
            "duration_seconds": duration,
            "bookmarks": bookmarks,
        }

    def create_live_bookmark(
        self, session_id: int, content: str = "", kind: str = "point"
    ) -> dict[str, Any]:
        session = self._session_or_raise(session_id)
        if str(session.get("status")) not in ACTIVE_RECORDING_STATUSES:
            raise InactiveRecordingError("이미 종료된 방송에는 현재 시점을 체크할 수 없습니다.")
        normalized_kind = normalize_bookmark_kind(kind)
        now = utc_now_iso()
        self.db.execute(
            """
            INSERT INTO video_bookmarks
              (session_id, kind, marked_at, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                normalized_kind,
                now,
                normalize_bookmark_content(content),
                now,
                now,
            ),
        )
        return self.live_collection(session_id)

    def _raw_media_offset(self, media: dict[str, Any], display_value: Any) -> float:
        display_offset = validate_offset_seconds(display_value)
        duration = self._duration(media)
        if duration is not None:
            display_offset = min(display_offset, duration)
        shift = normalize_bookmark_shift(media.get("bookmark_shift_seconds") or 0)
        return display_offset - shift

    def create_media_bookmark(
        self,
        media_id: int,
        display_offset_seconds: Any,
        content: str = "",
        kind: str = "point",
    ) -> dict[str, Any]:
        media = self._media_or_raise(media_id)
        raw_offset = self._raw_media_offset(media, display_offset_seconds)
        normalized_kind = normalize_bookmark_kind(kind)
        now = utc_now_iso()
        self.db.execute(
            """
            INSERT INTO video_bookmarks
              (session_id, media_id, kind, offset_seconds, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(media["session_id"]) if media.get("session_id") is not None else None,
                media_id,
                normalized_kind,
                raw_offset,
                normalize_bookmark_content(content),
                now,
                now,
            ),
        )
        return self.media_collection(media_id)

    def _assert_active_live(self, media_id: int | None, session_id: int | None) -> None:
        if media_id is not None or session_id is None:
            raise BookmarkValidationError("현재 방송 시점은 녹화 중 북마크에만 사용할 수 있습니다.")
        session = self._session_or_raise(session_id)
        if str(session.get("status")) not in ACTIVE_RECORDING_STATUSES:
            raise InactiveRecordingError("방송이 종료되어 현재 시점으로 바꿀 수 없습니다.")

    def _normalize_range_order(self, bookmark_id: int, *, include_current: bool) -> None:
        row = self._bookmark_or_raise(bookmark_id)
        if normalize_bookmark_kind(row.get("kind")) != "range":
            return
        start, end, _, _ = self._row_offsets(row, include_current=include_current)
        if end is None or end >= start:
            return
        self.db.execute(
            """
            UPDATE video_bookmarks
            SET marked_at = ?, offset_seconds = ?,
                end_marked_at = ?, end_offset_seconds = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                row.get("end_marked_at"),
                row.get("end_offset_seconds"),
                row.get("marked_at"),
                row.get("offset_seconds"),
                utc_now_iso(),
                bookmark_id,
            ),
        )

    def update_bookmark(
        self,
        bookmark_id: int,
        *,
        kind: str | None = None,
        kind_provided: bool = False,
        content: str | None = None,
        content_provided: bool = False,
        display_offset_seconds: Any = None,
        offset_provided: bool = False,
        end_display_offset_seconds: Any = None,
        end_offset_provided: bool = False,
        use_current_live_time: bool = False,
        use_current_live_end_time: bool = False,
    ) -> dict[str, Any]:
        row = self._bookmark_or_raise(bookmark_id)
        updates: list[str] = []
        params: list[Any] = []
        media_id = int(row["media_id"]) if row.get("media_id") is not None else None
        session_id = int(row["session_id"]) if row.get("session_id") is not None else None
        target_kind = normalize_bookmark_kind(kind if kind_provided else row.get("kind"))

        if kind_provided:
            updates.append("kind = ?")
            params.append(target_kind)
            if target_kind == "point":
                updates.extend(["end_marked_at = NULL", "end_offset_seconds = NULL"])
        if content_provided:
            updates.append("content = ?")
            params.append(normalize_bookmark_content(content or ""))

        if use_current_live_time:
            self._assert_active_live(media_id, session_id)
            updates.extend(["marked_at = ?", "offset_seconds = NULL"])
            params.append(utc_now_iso())
        elif offset_provided:
            if media_id is not None:
                raw_start = self._raw_media_offset(
                    self._media_or_raise(media_id), display_offset_seconds
                )
            else:
                raw_start = validate_offset_seconds(display_offset_seconds)
            updates.append("offset_seconds = ?")
            params.append(raw_start)

        if use_current_live_end_time:
            if target_kind != "range":
                raise BookmarkValidationError("구간 북마크에만 끝 시각을 설정할 수 있습니다.")
            self._assert_active_live(media_id, session_id)
            updates.extend(["end_marked_at = ?", "end_offset_seconds = NULL"])
            params.append(utc_now_iso())
        elif end_offset_provided:
            if target_kind != "range":
                raise BookmarkValidationError("구간 북마크에만 끝 시각을 설정할 수 있습니다.")
            if end_display_offset_seconds is None:
                updates.extend(["end_marked_at = NULL", "end_offset_seconds = NULL"])
            else:
                if media_id is not None:
                    raw_end = self._raw_media_offset(
                        self._media_or_raise(media_id), end_display_offset_seconds
                    )
                else:
                    raw_end = validate_offset_seconds(end_display_offset_seconds)
                updates.extend(["end_marked_at = NULL", "end_offset_seconds = ?"])
                params.append(raw_end)

        if not updates:
            raise BookmarkValidationError("수정할 북마크 값이 없습니다.")
        updates.append("updated_at = ?")
        params.extend([utc_now_iso(), bookmark_id])
        self.db.execute(
            f"UPDATE video_bookmarks SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        self._normalize_range_order(bookmark_id, include_current=media_id is None)
        return (
            self.media_collection(media_id)
            if media_id is not None
            else self.live_collection(int(session_id))
        )

    def delete_bookmark(self, bookmark_id: int) -> dict[str, Any]:
        row = self._bookmark_or_raise(bookmark_id)
        media_id = int(row["media_id"]) if row.get("media_id") is not None else None
        session_id = int(row["session_id"]) if row.get("session_id") is not None else None
        self.db.execute("DELETE FROM video_bookmarks WHERE id = ?", (bookmark_id,))
        return (
            self.media_collection(media_id)
            if media_id is not None
            else self.live_collection(int(session_id))
        )

    def set_media_shift(
        self,
        media_id: int,
        shift_seconds: Any = 0,
        *,
        reset_to_channel_default: bool = False,
    ) -> dict[str, Any]:
        media = self._media_or_raise(media_id)
        if reset_to_channel_default:
            _, normalized = self._channel_defaults(media)
        else:
            normalized = normalize_bookmark_shift(shift_seconds)
        self.db.execute(
            """
            UPDATE media_items
            SET bookmark_shift_seconds = ?, updated_at = ?
            WHERE id = ?
            """,
            (normalized, utc_now_iso(), media_id),
        )
        return self.media_collection(media_id)

    def set_media_chat_delay(
        self,
        media_id: int,
        delay_seconds: Any = 0,
        *,
        reset_to_channel_default: bool = False,
    ) -> dict[str, Any]:
        media = self._media_or_raise(media_id)
        if reset_to_channel_default:
            normalized, _ = self._channel_defaults(media)
        else:
            normalized = normalize_sync_seconds(delay_seconds, "채팅 보정값")
        self.db.execute(
            "UPDATE media_items SET chat_delay_seconds = ?, updated_at = ? WHERE id = ?",
            (normalized, utc_now_iso(), media_id),
        )
        return self.media_sync(media_id)
