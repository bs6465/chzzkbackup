from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiofiles

from . import config
from .chat_csv_migrate import STANDARD_CSV_FIELDS
from .logger import logger
from .twitcasting_api import get_comments
from .utils import KST, kst_iso


class TwitCastingChatCapture:
    def __init__(
        self,
        movie_id: str,
        access_token: str,
        jsonl_path: Path,
        csv_path: Path,
        recording_started_at: datetime,
        comments_fetcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.movie_id = movie_id
        self.access_token = access_token
        self.jsonl_path = jsonl_path
        self.csv_path = csv_path
        self.recording_started_at = recording_started_at
        self.comments_fetcher = comments_fetcher or get_comments
        self._seen_ids: set[str] = set()
        self._slice_id: str | None = None

    async def run(self, stop: Callable[[], bool]) -> None:
        if not self.access_token:
            logger.warning("TwitCasting chat capture skipped for %s: access token is not set", self.movie_id)
            return

        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiofiles.open(self.jsonl_path, "a", encoding="utf-8") as jsonl_file:
                with self.csv_path.open("a", encoding="utf-8-sig", newline="") as csv_file:
                    csv_writer = csv.DictWriter(csv_file, fieldnames=STANDARD_CSV_FIELDS)
                    if csv_file.tell() == 0:
                        csv_writer.writeheader()
                        csv_file.flush()

                    while not stop():
                        try:
                            rows = await self._fetch_new_comments()
                            for row in rows:
                                await jsonl_file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                                csv_writer.writerow(
                                    {
                                        "type": row["type"],
                                        "timestamp": row["timestamp"],
                                        "offset_seconds": row["offset_seconds"],
                                        "nickname": row["nickname"],
                                        "content": row["content"],
                                    }
                                )
                            if rows:
                                await jsonl_file.flush()
                                csv_file.flush()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            if stop():
                                break
                            logger.warning("TwitCasting chat capture error for %s: %s", self.movie_id, exc)

                        await self._sleep_until_stop(stop, config.TWITCASTING_CHAT_POLL_SECONDS)
                    logger.info("TwitCasting chat capture stopped for %s", self.movie_id)
        except asyncio.CancelledError:
            raise

    async def _fetch_new_comments(self) -> list[dict[str, Any]]:
        data = await self.comments_fetcher(
            self.movie_id,
            self.access_token,
            limit=50,
            slice_id=self._slice_id,
        )
        comments = data.get("comments") if isinstance(data, dict) else None
        if not isinstance(comments, list):
            return []

        newest_id = self._slice_id
        rows: list[dict[str, Any]] = []
        for comment in sorted(comments, key=self._comment_sort_key):
            if not isinstance(comment, dict):
                continue
            comment_id = str(comment.get("id") or "")
            if comment_id:
                newest_id = self._max_comment_id(newest_id, comment_id)
                if comment_id in self._seen_ids:
                    continue
                self._seen_ids.add(comment_id)

            created_at = self._comment_created_at(comment)
            if created_at < self.recording_started_at.astimezone(KST):
                continue
            rows.append(self._comment_to_row(comment, created_at))

        self._slice_id = newest_id
        return rows

    def _comment_to_row(self, comment: dict[str, Any], created_at: datetime) -> dict[str, Any]:
        from_user = comment.get("from_user")
        if not isinstance(from_user, dict):
            from_user = {}
        offset = max(0.0, (created_at - self.recording_started_at.astimezone(KST)).total_seconds())
        return {
            "type": "chat",
            "timestamp": kst_iso(created_at),
            "offset_seconds": round(offset, 3),
            "nickname": from_user.get("name") or from_user.get("screen_id") or "",
            "content": comment.get("message") or "",
            "raw": comment,
        }

    def _comment_created_at(self, comment: dict[str, Any]) -> datetime:
        try:
            timestamp = float(comment.get("created"))
        except (TypeError, ValueError):
            return datetime.now(KST)
        return datetime.fromtimestamp(timestamp, tz=KST)

    def _comment_sort_key(self, comment: Any) -> tuple[float, int]:
        if not isinstance(comment, dict):
            return 0.0, 0
        try:
            created = float(comment.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        try:
            comment_id = int(comment.get("id") or 0)
        except (TypeError, ValueError):
            comment_id = 0
        return created, comment_id

    def _max_comment_id(self, left: str | None, right: str) -> str:
        try:
            if left is None or int(right) > int(left):
                return right
        except (TypeError, ValueError):
            if not left or right > left:
                return right
        return left

    async def _sleep_until_stop(self, stop: Callable[[], bool], seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not stop():
            delay = min(0.5, remaining)
            await asyncio.sleep(delay)
            remaining -= delay
