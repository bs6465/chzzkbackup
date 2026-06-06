from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .db import db
from .logger import logger


class MaintenanceWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

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
                db.cleanup_old_logs()
                self.cleanup_failed_temp_files()
            except Exception as exc:
                logger.warning("Maintenance failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                continue

    def cleanup_failed_temp_files(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.FAILED_TEMP_RETENTION_DAYS)
        for row in db.query_all(
            "SELECT temp_path, source_path FROM recording_sessions WHERE status = 'failed'"
        ):
            for key in ("temp_path", "source_path"):
                raw = row.get(key)
                if not raw:
                    continue
                path = Path(raw)
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                mtime = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                if mtime < cutoff:
                    path.unlink(missing_ok=True)
                    logger.info("Deleted old failed temp file: %s", path)
