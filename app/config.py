from __future__ import annotations

import os
from pathlib import Path


APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "./data")).resolve()
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./temp")).resolve()
FINAL_ROOT = Path(os.getenv("FINAL_ROOT", "/data/chzzk_backup")).resolve()
DB_PATH = APP_DATA_DIR / "chzzkbackup.sqlite3"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
NETWORK_ERROR_LOG_INTERVAL_SECONDS = int(os.getenv("NETWORK_ERROR_LOG_INTERVAL_SECONDS", "300"))
TWITCASTING_CHAT_POLL_SECONDS = int(os.getenv("TWITCASTING_CHAT_POLL_SECONDS", "5"))
UI_STATUS_INTERVAL_SECONDS = 3
UI_LOG_INTERVAL_SECONDS = 5
DISK_WARN_BYTES = int(os.getenv("DISK_WARN_BYTES", str(100 * 1024**3)))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
LOG_RETENTION_MAX_ROWS = int(os.getenv("LOG_RETENTION_MAX_ROWS", "1000"))
FAILED_TEMP_RETENTION_DAYS = 7

LIVE_DETAIL_API = "https://api.chzzk.naver.com/service/v3/channels/{channel_id}/live-detail"
CHANNEL_API = "https://api.chzzk.naver.com/service/v1/channels/{channel_id}"

STREAMLINK_PLUGIN_DIR = Path(__file__).resolve().parent / "plugin"
