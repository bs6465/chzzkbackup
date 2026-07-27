from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config
from .db import Database, db
from .logger import logger
from .utils import unique_path, utc_now_iso

KST = ZoneInfo("Asia/Seoul")
VALID_OVERRIDES = {"inherit", "forever", "scheduled"}


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def expiry_at_end_of_day(value: str) -> datetime:
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed, time(23, 59, 59), tzinfo=KST)


def within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


class RetentionService:
    def __init__(self, database: Database = db) -> None:
        self.db = database
        self._lock = threading.RLock()
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.last_trashed_count = 0
        self.last_purged_count = 0

    def policies(self) -> list[dict[str, Any]]:
        return self.db.query_all(
            """
            SELECT * FROM retention_policies
            ORDER BY CASE WHEN channel_id IS NULL THEN 1 ELSE 0 END,
                     platform, channel_name COLLATE NOCASE
            """
        )

    def set_policy(
        self,
        policy_key: str,
        *,
        retention_days: int | None,
        max_items: int | None,
        max_bytes: int | None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._set_policy(
                policy_key,
                retention_days=retention_days,
                max_items=max_items,
                max_bytes=max_bytes,
            )

    def _set_policy(
        self,
        policy_key: str,
        *,
        retention_days: int | None,
        max_items: int | None,
        max_bytes: int | None,
    ) -> dict[str, Any]:
        policy = self.db.query_one(
            "SELECT * FROM retention_policies WHERE policy_key = ?",
            (policy_key,),
        )
        if not policy:
            raise KeyError("Retention policy not found")
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE retention_policies
            SET retention_days = ?, max_items = ?, max_bytes = ?, updated_at = ?
            WHERE policy_key = ?
            """,
            (retention_days, max_items, max_bytes, now, policy_key),
        )
        return self.db.query_one(
            "SELECT * FROM retention_policies WHERE policy_key = ?",
            (policy_key,),
        ) or {}

    def set_media_override(
        self,
        media_ids: list[int],
        override: str,
        expires_on: str | None = None,
    ) -> int:
        with self._lock:
            return self._set_media_override(media_ids, override, expires_on)

    def _set_media_override(
        self,
        media_ids: list[int],
        override: str,
        expires_on: str | None = None,
    ) -> int:
        if override not in VALID_OVERRIDES:
            raise ValueError("Invalid retention override")
        expires_at: str | None = None
        if override == "scheduled":
            if not expires_on:
                raise ValueError("Expiration date is required")
            expires_at = expiry_at_end_of_day(expires_on).isoformat()
        if not media_ids:
            raise ValueError("Select at least one media item")
        placeholders = ",".join("?" for _ in media_ids)
        rows = self.db.query_all(
            f"SELECT id FROM media_items WHERE status='available' AND id IN ({placeholders})",
            tuple(media_ids),
        )
        if len(rows) != len(set(media_ids)):
            raise ValueError("One or more media items are unavailable")
        now = utc_now_iso()
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                f"""
                UPDATE media_items
                SET retention_override = ?, retention_expires_at = ?, updated_at = ?
                WHERE status='available' AND id IN ({placeholders})
                """,
                (override, expires_at, now, *media_ids),
            )
        return len(rows)

    def evaluate(
        self,
        *,
        now: datetime | None = None,
        policy_overrides: dict[str, dict[str, int | None]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._evaluate(now=now, policy_overrides=policy_overrides)

    def _evaluate(
        self,
        *,
        now: datetime | None = None,
        policy_overrides: dict[str, dict[str, int | None]] | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        policies = {
            str(row["policy_key"]): dict(row)
            for row in self.policies()
        }
        for key, values in (policy_overrides or {}).items():
            if key in policies:
                policies[key].update(values)

        rows = self.db.query_all(
            "SELECT * FROM media_items WHERE status = 'available'"
        )
        active_clip_ids = {
            int(row["media_id"])
            for row in self.db.query_all(
                """
                SELECT DISTINCT media_id FROM clip_jobs
                WHERE status IN ('queued', 'running') AND media_id IS NOT NULL
                """
            )
        }
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = str(row.get("retention_policy_key") or "")
            groups.setdefault(key, []).append(row)

        item_states: dict[int, dict[str, Any]] = {}
        candidates: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        by_policy: dict[str, dict[str, int]] = {
            key: {"candidate_count": 0, "candidate_bytes": 0, "deferred_count": 0}
            for key in policies
        }

        def started_sort_key(item: dict[str, Any]) -> tuple[datetime, int]:
            started = parse_datetime(item.get("started_at")) or datetime.max.replace(
                tzinfo=timezone.utc
            )
            return started, int(item["id"])

        for policy_key, items in groups.items():
            policy = policies.get(policy_key, {})
            inherited: list[dict[str, Any]] = []
            reasons: dict[int, str] = {}
            retention_days = policy.get("retention_days")
            max_items = policy.get("max_items")
            max_bytes = policy.get("max_bytes")
            no_limits = not any(
                value is not None for value in (retention_days, max_items, max_bytes)
            )

            for item in items:
                media_id = int(item["id"])
                override = str(item.get("retention_override") or "inherit")
                state = {
                    "media_id": media_id,
                    "policy_key": policy_key,
                    "override": override,
                    "category": "inherit",
                    "expires_at": None,
                    "candidate": False,
                    "deferred": False,
                    "reason": None,
                }
                if override == "forever":
                    state["category"] = "forever"
                    item_states[media_id] = state
                    continue
                if override == "scheduled":
                    expires_at = parse_datetime(item.get("retention_expires_at"))
                    state["expires_at"] = expires_at.isoformat() if expires_at else None
                    if expires_at and expires_at <= now:
                        state["category"] = "expiring"
                        reasons[media_id] = "지정 보관일 만료"
                    else:
                        state["category"] = "scheduled"
                    item_states[media_id] = state
                    continue

                if no_limits:
                    state["category"] = "forever"
                if retention_days is not None:
                    started = parse_datetime(item.get("started_at"))
                    if started:
                        expires_at = started + timedelta(days=int(retention_days))
                        state["expires_at"] = expires_at.isoformat()
                        if expires_at <= now:
                            state["category"] = "expiring"
                            reasons[media_id] = f"{int(retention_days)}일 보관기간 만료"
                        elif expires_at <= now + timedelta(days=7):
                            state["category"] = "expiring"
                inherited.append(item)
                item_states[media_id] = state

            remaining = sorted(
                [item for item in inherited if int(item["id"]) not in reasons],
                key=started_sort_key,
            )
            if max_items is not None and len(remaining) > int(max_items):
                excess = len(remaining) - int(max_items)
                for item in remaining[:excess]:
                    reasons[int(item["id"])] = f"최대 {int(max_items)}개 초과"
                remaining = remaining[excess:]

            if max_bytes is not None:
                total_bytes = sum(int(item.get("size_bytes") or 0) for item in remaining)
                index = 0
                while total_bytes > int(max_bytes) and index < len(remaining):
                    item = remaining[index]
                    media_id = int(item["id"])
                    total_bytes -= int(item.get("size_bytes") or 0)
                    reasons[media_id] = "영상 용량 한도 초과"
                    index += 1

            for item in items:
                media_id = int(item["id"])
                reason = reasons.get(media_id)
                if not reason:
                    continue
                state = item_states[media_id]
                state["candidate"] = True
                state["reason"] = reason
                candidate = {
                    **item,
                    "policy_key": policy_key,
                    "retention_reason": reason,
                }
                stats = by_policy.setdefault(
                    policy_key,
                    {"candidate_count": 0, "candidate_bytes": 0, "deferred_count": 0},
                )
                if media_id in active_clip_ids:
                    state["deferred"] = True
                    stats["deferred_count"] += 1
                    deferred.append(candidate)
                    continue
                stats["candidate_count"] += 1
                stats["candidate_bytes"] += int(item.get("size_bytes") or 0)
                candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                parse_datetime(item.get("started_at"))
                or datetime.max.replace(tzinfo=timezone.utc),
                int(item["id"]),
            )
        )
        return {
            "items": item_states,
            "candidates": candidates,
            "deferred": deferred,
            "candidate_count": len(candidates),
            "candidate_bytes": sum(
                int(item.get("size_bytes") or 0) for item in candidates
            ),
            "by_policy": by_policy,
        }

    def preview_policy(
        self,
        policy_key: str,
        *,
        retention_days: int | None,
        max_items: int | None,
        max_bytes: int | None,
    ) -> dict[str, int]:
        snapshot = self.evaluate(
            policy_overrides={
                policy_key: {
                    "retention_days": retention_days,
                    "max_items": max_items,
                    "max_bytes": max_bytes,
                }
            }
        )
        return snapshot["by_policy"].get(
            policy_key,
            {"candidate_count": 0, "candidate_bytes": 0, "deferred_count": 0},
        )

    def run_cleanup(self) -> dict[str, int]:
        with self._lock:
            trashed = 0
            purged = 0
            try:
                purged = self.purge_expired()
                snapshot = self.evaluate()
                for item in snapshot["candidates"]:
                    if self.trash_media(int(item["id"]), str(item["retention_reason"])):
                        trashed += 1
                self.last_run_at = utc_now_iso()
                self.last_error = None
                self.last_trashed_count = trashed
                self.last_purged_count = purged
                return {"trashed": trashed, "purged": purged}
            except Exception as exc:
                self.last_run_at = utc_now_iso()
                self.last_error = str(exc)
                raise

    def trash_media(self, media_id: int, reason: str) -> bool:
        with self._lock:
            return self._trash_media(media_id, reason)

    def _trash_media(self, media_id: int, reason: str) -> bool:
        item = self.db.get_media_item(media_id)
        if not item or item.get("status") != "available":
            return False
        active = self.db.query_one(
            """
            SELECT id FROM clip_jobs
            WHERE media_id = ? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (media_id,),
        )
        if active:
            return False

        video_path = Path(str(item["video_path"]))
        if not within_root(video_path, config.FINAL_ROOT) or not video_path.is_file():
            logger.warning("Retention skipped missing or unsafe media: %s", video_path)
            return False
        trash_dir = config.FINAL_ROOT / config.TRASH_DIR_NAME / str(media_id)
        trash_dir.mkdir(parents=True, exist_ok=True)
        sources = [
            ("trash_video_path", video_path, trash_dir / "video.mp4"),
            (
                "trash_chat_jsonl_path",
                Path(str(item["chat_jsonl_path"])) if item.get("chat_jsonl_path") else None,
                trash_dir / "chat.jsonl",
            ),
            (
                "trash_chat_csv_path",
                Path(str(item["chat_csv_path"])) if item.get("chat_csv_path") else None,
                trash_dir / "chat.csv",
            ),
        ]
        moved: list[tuple[Path, Path]] = []
        trash_paths: dict[str, str | None] = {}
        try:
            for key, source, destination in sources:
                if not source or not source.exists():
                    trash_paths[key] = None
                    continue
                if not within_root(source, config.FINAL_ROOT):
                    raise ValueError(f"Unsafe retention path: {source}")
                if destination.exists():
                    raise FileExistsError(f"Trash destination already exists: {destination}")
                source.replace(destination)
                moved.append((destination, source))
                trash_paths[key] = str(destination)
            now = datetime.now(timezone.utc)
            with self.db._lock, self.db._conn:
                self.db._conn.execute(
                    """
                    INSERT INTO media_trash
                      (media_id, trash_video_path, trash_chat_jsonl_path,
                       trash_chat_csv_path, trashed_at, purge_after, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_id,
                        trash_paths.get("trash_video_path"),
                        trash_paths.get("trash_chat_jsonl_path"),
                        trash_paths.get("trash_chat_csv_path"),
                        now.isoformat(),
                        (now + timedelta(days=config.TRASH_RETENTION_DAYS)).isoformat(),
                        reason,
                    ),
                )
                self.db._conn.execute(
                    "UPDATE media_items SET status='trashed', updated_at=? WHERE id=?",
                    (now.isoformat(), media_id),
                )
            logger.info("Media moved to retention trash: %s (%s)", video_path, reason)
            return True
        except Exception:
            for current, original in reversed(moved):
                if current.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    current.replace(original)
            raise

    def trash_items(self) -> list[dict[str, Any]]:
        return self.db.query_all(
            """
            SELECT media_items.*, media_trash.trash_video_path,
                   media_trash.trash_chat_jsonl_path,
                   media_trash.trash_chat_csv_path,
                   media_trash.trashed_at, media_trash.purge_after,
                   media_trash.reason AS retention_reason
            FROM media_trash
            JOIN media_items ON media_items.id = media_trash.media_id
            ORDER BY media_trash.purge_after
            """
        )

    def restore_media(self, media_id: int) -> dict[str, Any]:
        with self._lock:
            return self._restore_media(media_id)

    def _restore_media(self, media_id: int) -> dict[str, Any]:
        row = self.db.query_one(
            """
            SELECT media_items.*, media_trash.trash_video_path,
                   media_trash.trash_chat_jsonl_path,
                   media_trash.trash_chat_csv_path
            FROM media_trash
            JOIN media_items ON media_items.id = media_trash.media_id
            WHERE media_items.id = ?
            """,
            (media_id,),
        )
        if not row:
            raise KeyError("Trash item not found")
        trash_video = Path(str(row["trash_video_path"])) if row.get("trash_video_path") else None
        if not trash_video or not trash_video.is_file():
            raise FileNotFoundError("Trashed video file is missing")
        pairs = [
            ("video_path", trash_video, Path(str(row["video_path"]))),
            (
                "chat_jsonl_path",
                Path(str(row["trash_chat_jsonl_path"]))
                if row.get("trash_chat_jsonl_path")
                else None,
                Path(str(row["chat_jsonl_path"])) if row.get("chat_jsonl_path") else None,
            ),
            (
                "chat_csv_path",
                Path(str(row["trash_chat_csv_path"]))
                if row.get("trash_chat_csv_path")
                else None,
                Path(str(row["chat_csv_path"])) if row.get("chat_csv_path") else None,
            ),
        ]
        restored: list[tuple[Path, Path]] = []
        updates: dict[str, str | None] = {}
        try:
            for key, source, target in pairs:
                if not source or not source.exists() or not target:
                    continue
                if not within_root(source, config.FINAL_ROOT):
                    raise ValueError(f"Unsafe trash source path: {source}")
                if not within_root(target, config.FINAL_ROOT):
                    raise ValueError(f"Unsafe restore target path: {target}")
                destination = unique_path(target) if target.exists() else target
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
                restored.append((destination, source))
                updates[key] = str(destination)
            with self.db._lock, self.db._conn:
                self.db._conn.execute(
                    """
                    UPDATE media_items
                    SET video_path=?, chat_jsonl_path=?, chat_csv_path=?,
                        status='available', retention_override='forever',
                        retention_expires_at=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        updates.get("video_path", row["video_path"]),
                        updates.get("chat_jsonl_path", row.get("chat_jsonl_path")),
                        updates.get("chat_csv_path", row.get("chat_csv_path")),
                        utc_now_iso(),
                        media_id,
                    ),
                )
                self.db._conn.execute(
                    "DELETE FROM media_trash WHERE media_id=?",
                    (media_id,),
                )
            self._remove_empty_trash_dir(media_id)
            logger.info("Media restored from retention trash: %s", media_id)
            return self.db.get_media_item(media_id) or {}
        except Exception:
            for current, original in reversed(restored):
                if current.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    current.replace(original)
            raise

    def purge_media(self, media_id: int) -> bool:
        with self._lock:
            return self._purge_media(media_id)

    def _purge_media(self, media_id: int) -> bool:
        row = self.db.query_one(
            """
            SELECT media_items.*, media_trash.trash_video_path,
                   media_trash.trash_chat_jsonl_path,
                   media_trash.trash_chat_csv_path,
                   media_trash.reason AS retention_reason
            FROM media_trash
            JOIN media_items ON media_items.id = media_trash.media_id
            WHERE media_items.id = ?
            """,
            (media_id,),
        )
        if not row:
            return False
        for key in (
            "trash_video_path",
            "trash_chat_jsonl_path",
            "trash_chat_csv_path",
            "thumbnail_path",
        ):
            raw = row.get(key)
            if not raw:
                continue
            path = Path(str(raw))
            if key.startswith("trash_") and not within_root(path, config.FINAL_ROOT):
                raise ValueError(f"Unsafe trash path: {path}")
            if key == "thumbnail_path" and not within_root(path, config.APP_DATA_DIR):
                raise ValueError(f"Unsafe thumbnail path: {path}")
            path.unlink(missing_ok=True)
        deleted_at = utc_now_iso()
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                """
                INSERT INTO media_deletion_history
                  (original_media_id, platform, channel_name, title, started_at,
                   size_bytes, reason, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_id,
                    row["platform"],
                    row["channel_name"],
                    row["title"],
                    row["started_at"],
                    int(row.get("size_bytes") or 0),
                    row["retention_reason"],
                    deleted_at,
                ),
            )
            self.db._conn.execute("DELETE FROM media_items WHERE id=?", (media_id,))
        self._remove_empty_trash_dir(media_id)
        logger.info("Media permanently deleted from retention trash: %s", media_id)
        return True

    def purge_expired(self, now: datetime | None = None) -> int:
        with self._lock:
            return self._purge_expired(now)

    def _purge_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        rows = self.db.query_all(
            "SELECT media_id FROM media_trash WHERE purge_after <= ? ORDER BY purge_after",
            (now.isoformat(),),
        )
        count = 0
        for row in rows:
            count += int(self.purge_media(int(row["media_id"])))
        return count

    def purge_all(self) -> int:
        with self._lock:
            rows = self.db.query_all("SELECT media_id FROM media_trash ORDER BY media_id")
            count = 0
            for row in rows:
                count += int(self.purge_media(int(row["media_id"])))
            return count

    def recent_deletions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM media_deletion_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def status(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "last_trashed_count": self.last_trashed_count,
            "last_purged_count": self.last_purged_count,
            "trash_count": int(
                (
                    self.db.query_one(
                        "SELECT count(*) AS count FROM media_trash"
                    )
                    or {}
                ).get("count")
                or 0
            ),
        }

    def _remove_empty_trash_dir(self, media_id: int) -> None:
        directory = config.FINAL_ROOT / config.TRASH_DIR_NAME / str(media_id)
        try:
            directory.rmdir()
            directory.parent.rmdir()
        except OSError:
            pass


retention = RetentionService()
