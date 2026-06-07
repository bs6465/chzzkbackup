from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config
from .utils import sanitize_cookie_value, sanitize_name, unique_path, utc_now_iso


class Database:
    def __init__(self, path: Path = config.DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channels (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recording_sessions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  channel_id TEXT NOT NULL,
                  channel_name TEXT NOT NULL,
                  live_id TEXT,
                  live_title TEXT,
                  started_at TEXT NOT NULL,
                  ended_at TEXT,
                  status TEXT NOT NULL,
                  temp_path TEXT,
                  source_path TEXT,
                  final_path TEXT,
                  chat_jsonl_path TEXT,
                  chat_csv_path TEXT,
                  error TEXT,
                  FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS encode_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id INTEGER NOT NULL,
                  source_path TEXT NOT NULL,
                  final_path TEXT NOT NULL,
                  status TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  duration_seconds REAL,
                  encoded_seconds REAL DEFAULT 0,
                  progress_percent REAL DEFAULT 0,
                  speed TEXT,
                  eta_seconds REAL,
                  progress_updated_at TEXT,
                  FOREIGN KEY(session_id) REFERENCES recording_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS app_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  level TEXT NOT NULL,
                  message TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_columns(
                "encode_jobs",
                {
                    "duration_seconds": "REAL",
                    "encoded_seconds": "REAL DEFAULT 0",
                    "progress_percent": "REAL DEFAULT 0",
                    "speed": "TEXT",
                    "eta_seconds": "REAL",
                    "progress_updated_at": "TEXT",
                },
            )
            self._chmod_private()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _chmod_private(self) -> None:
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def get_channels(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM channels"
        params: tuple[Any, ...] = ()
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        return self.query_all(sql, params)

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM channels WHERE id = ?", (channel_id,))

    def upsert_channel(self, channel_id: str, name: str, active: bool = True) -> None:
        now = utc_now_iso()
        self.execute(
            """
            INSERT INTO channels (id, name, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              updated_at = excluded.updated_at
            """,
            (channel_id, name, int(active), now, now),
        )

    def rename_channel(self, channel_id: str, name: str) -> None:
        self.execute(
            "UPDATE channels SET name = ?, updated_at = ? WHERE id = ?",
            (name, utc_now_iso(), channel_id),
        )

    def set_channel_active(self, channel_id: str, active: bool) -> None:
        self.execute(
            "UPDATE channels SET active = ?, updated_at = ? WHERE id = ?",
            (int(active), utc_now_iso(), channel_id),
        )

    def delete_channel(self, channel_id: str) -> None:
        self.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        now = utc_now_iso()
        self.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, payload, now),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_tokens(self, nid_ses: str, nid_aut: str) -> None:
        self.set_setting(
            "tokens",
            {
                "NID_SES": sanitize_cookie_value(nid_ses),
                "NID_AUT": sanitize_cookie_value(nid_aut),
            },
        )

    def get_tokens(self) -> dict[str, str]:
        tokens = self.get_setting("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
        return {
            "NID_SES": sanitize_cookie_value(tokens.get("NID_SES", "")),
            "NID_AUT": sanitize_cookie_value(tokens.get("NID_AUT", "")),
        }

    def create_session(
        self,
        channel_id: str,
        channel_name: str,
        live_id: str | None,
        live_title: str,
        started_at: str,
        temp_path: Path,
        chat_jsonl_path: Path,
        chat_csv_path: Path,
    ) -> int:
        cur = self.execute(
            """
            INSERT INTO recording_sessions
            (channel_id, channel_name, live_id, live_title, started_at, status, temp_path, chat_jsonl_path, chat_csv_path)
            VALUES (?, ?, ?, ?, ?, 'recording', ?, ?, ?)
            """,
            (
                channel_id,
                channel_name,
                live_id,
                live_title,
                started_at,
                str(temp_path),
                str(chat_jsonl_path),
                str(chat_csv_path),
            ),
        )
        return int(cur.lastrowid)

    def finish_session(self, session_id: int, source_path: Path | None, status: str = "queued") -> None:
        self.execute(
            """
            UPDATE recording_sessions
            SET ended_at = ?, status = ?, source_path = ?
            WHERE id = ?
            """,
            (utc_now_iso(), status, str(source_path) if source_path else None, session_id),
        )

    def update_session_status(
        self,
        session_id: int,
        status: str,
        *,
        final_path: Path | None = None,
        error: str | None = None,
    ) -> None:
        self.execute(
            """
            UPDATE recording_sessions
            SET status = ?, final_path = COALESCE(?, final_path), error = COALESCE(?, error)
            WHERE id = ?
            """,
            (status, str(final_path) if final_path else None, error, session_id),
        )

    def add_encode_job(self, session_id: int, source_path: Path, final_path: Path) -> int:
        cur = self.execute(
            """
            INSERT INTO encode_jobs (session_id, source_path, final_path, status, created_at)
            VALUES (?, ?, ?, 'queued', ?)
            """,
            (session_id, str(source_path), str(final_path), utc_now_iso()),
        )
        return int(cur.lastrowid)

    def next_encode_job(self) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM encode_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        )

    def update_encode_job(self, job_id: int, status: str, error: str | None = None) -> None:
        fields = ["status = ?"]
        params: list[Any] = [status]
        now = utc_now_iso()
        if status == "running":
            fields.append("started_at = ?")
            params.append(now)
            fields.extend(
                [
                    "finished_at = NULL",
                    "error = NULL",
                    "duration_seconds = NULL",
                    "encoded_seconds = 0",
                    "progress_percent = 0",
                    "speed = NULL",
                    "eta_seconds = NULL",
                    "progress_updated_at = ?",
                ]
            )
            params.append(now)
        if status in {"completed", "failed"}:
            fields.append("finished_at = ?")
            params.append(now)
        if status == "completed":
            fields.extend(
                [
                    "encoded_seconds = COALESCE(duration_seconds, encoded_seconds)",
                    "progress_percent = 100",
                    "eta_seconds = 0",
                    "progress_updated_at = ?",
                ]
            )
            params.append(now)
        if error:
            fields.append("error = ?")
            params.append(error)
        params.append(job_id)
        self.execute(f"UPDATE encode_jobs SET {', '.join(fields)} WHERE id = ?", tuple(params))

    def update_encode_progress(
        self,
        job_id: int,
        *,
        duration_seconds: float | None,
        encoded_seconds: float | None,
        progress_percent: float | None,
        speed: str | None,
        eta_seconds: float | None,
    ) -> None:
        self.execute(
            """
            UPDATE encode_jobs
            SET duration_seconds = COALESCE(?, duration_seconds),
                encoded_seconds = COALESCE(?, encoded_seconds),
                progress_percent = COALESCE(?, progress_percent),
                speed = COALESCE(?, speed),
                eta_seconds = ?,
                progress_updated_at = ?
            WHERE id = ?
            """,
            (
                duration_seconds,
                encoded_seconds,
                progress_percent,
                speed,
                eta_seconds,
                utc_now_iso(),
                job_id,
            ),
        )

    def requeue_encode_job(self, job_id: int, session_id: int) -> None:
        self.execute(
            """
            UPDATE encode_jobs
            SET status = 'queued',
                started_at = NULL,
                finished_at = NULL,
                error = NULL,
                duration_seconds = NULL,
                encoded_seconds = 0,
                progress_percent = 0,
                speed = NULL,
                eta_seconds = NULL,
                progress_updated_at = NULL
            WHERE id = ?
            """,
            (job_id,),
        )
        self.update_session_status(session_id, "queued")

    def recover_interrupted_sessions(self) -> dict[str, int]:
        recovered = {"queued": 0, "failed": 0}
        rows = self.query_all("SELECT * FROM recording_sessions WHERE status = 'recording'")
        now = utc_now_iso()
        for row in rows:
            session_id = int(row["id"])
            temp_path = Path(row["temp_path"])
            if temp_path.exists() and temp_path.stat().st_size > 0:
                source_path = unique_path(temp_path.with_suffix(""))
                temp_path.replace(source_path)
                video_dir = config.FINAL_ROOT / sanitize_name(row["channel_name"], "unknown")
                video_dir.mkdir(parents=True, exist_ok=True)
                final_path = unique_path(video_dir / f"{source_path.stem}.mp4")
                self.finish_session(session_id, source_path, "queued")
                self.add_encode_job(session_id, source_path, final_path)
                recovered["queued"] += 1
            else:
                self.execute(
                    """
                    UPDATE recording_sessions
                    SET status = 'failed', ended_at = ?, error = ?
                    WHERE id = ?
                    """,
                    (now, "Recording was interrupted by application restart", session_id),
                )
                recovered["failed"] += 1
        return recovered

    def recover_interrupted_encode_jobs(self) -> dict[str, int]:
        recovered = {"queued": 0, "completed": 0, "failed": 0}
        rows = self.query_all("SELECT * FROM encode_jobs WHERE status = 'running'")
        for row in rows:
            job_id = int(row["id"])
            session_id = int(row["session_id"])
            source_path = Path(row["source_path"])
            final_path = Path(row["final_path"])
            if source_path.exists():
                self.requeue_encode_job(job_id, session_id)
                recovered["queued"] += 1
            elif final_path.exists() and final_path.stat().st_size > 0:
                self.update_encode_job(job_id, "completed")
                self.update_session_status(session_id, "completed", final_path=final_path)
                recovered["completed"] += 1
            else:
                error = "Encoding was interrupted by application restart and source file is missing"
                self.update_encode_job(job_id, "failed", error)
                self.update_session_status(session_id, "failed", error=error)
                recovered["failed"] += 1
        return recovered

    def active_sessions(self) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM recording_sessions WHERE status = 'recording' ORDER BY started_at DESC"
        )

    def recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM recording_sessions ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def encode_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM encode_jobs ORDER BY id DESC LIMIT ?", (limit,))

    def add_log(self, level: str, message: str) -> None:
        self.execute(
            "INSERT INTO app_logs (level, message, created_at) VALUES (?, ?, ?)",
            (level.upper(), message, utc_now_iso()),
        )

    def recent_logs(self, limit: int = 80) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM app_logs ORDER BY id DESC LIMIT ?", (limit,))

    def cleanup_old_logs(
        self,
        days: int = config.LOG_RETENTION_DAYS,
        max_rows: int = config.LOG_RETENTION_MAX_ROWS,
    ) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        self.execute("DELETE FROM app_logs WHERE created_at < ?", (cutoff.isoformat(),))
        if max_rows > 0:
            self.execute(
                """
                DELETE FROM app_logs
                WHERE id NOT IN (
                  SELECT id FROM app_logs
                  ORDER BY id DESC
                  LIMIT ?
                )
                """,
                (max_rows,),
            )


db = Database()
