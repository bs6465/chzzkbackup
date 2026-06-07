from __future__ import annotations

import csv
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aiofiles

from .logger import logger
from .utils import kst_iso, now_kst


class ChatCapture:
    def __init__(
        self,
        channel_id: str,
        tokens: dict[str, str],
        jsonl_path: Path,
        csv_path: Path,
        recording_started_at: datetime,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.channel_id = channel_id
        self.tokens = tokens
        self.jsonl_path = jsonl_path
        self.csv_path = csv_path
        self.recording_started_at = recording_started_at
        self.client_factory = client_factory
        self._csv_file = None
        self._csv_writer: csv.DictWriter[str] | None = None

    async def _write_event(self, event_type: str, message: Any) -> None:
        ts = now_kst()
        offset = max(0.0, (ts - self.recording_started_at).total_seconds())
        payload = self._message_to_dict(message)
        row = {
            "type": event_type,
            "timestamp": kst_iso(ts),
            "offset_seconds": round(offset, 3),
            "nickname": payload.get("nickname") or payload.get("profile", {}).get("nickname", ""),
            "content": payload.get("content") or payload.get("message") or "",
            "raw": payload,
        }

        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self.jsonl_path, "a", encoding="utf-8") as f:
            await f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        if self._csv_writer:
            self._csv_writer.writerow(
                {
                    "type": row["type"],
                    "timestamp": row["timestamp"],
                    "offset_seconds": row["offset_seconds"],
                    "nickname": row["nickname"],
                    "content": row["content"],
                    "raw_json": json.dumps(payload, ensure_ascii=False, default=str),
                }
            )
            self._csv_file.flush()

    def _message_to_dict(self, message: Any) -> dict[str, Any]:
        if isinstance(message, dict):
            return message
        if hasattr(message, "model_dump"):
            return message.model_dump()
        if hasattr(message, "__dict__"):
            return {
                key: value
                for key, value in vars(message).items()
                if not key.startswith("_")
            }
        return {"content": str(message)}

    def _bind_handler(self, chat: Any, attr: str, event_type: str) -> None:
        registrar = getattr(chat, attr, None)
        if not callable(registrar):
            return

        async def async_handler(message: Any) -> None:
            await self._write_event(event_type, message)

        def sync_handler(message: Any) -> None:
            import asyncio

            asyncio.create_task(self._write_event(event_type, message))

        try:
            registrar(async_handler)
        except Exception:
            try:
                registrar(sync_handler)
            except Exception:
                logger.warning("Could not register chat handler: %s", attr)

    async def _sleep_until_stop(self, stop: Callable[[], bool], seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not stop():
            delay = min(0.5, remaining)
            await asyncio.sleep(delay)
            remaining -= delay

    async def run(self, stop: Callable[[], bool]) -> None:
        client_factory = self.client_factory
        if client_factory is None:
            try:
                from chzzk.unofficial import AsyncUnofficialChatClient
            except Exception as exc:
                logger.warning("chzzk-python chat client unavailable: %s", exc)
                return
            client_factory = AsyncUnofficialChatClient

        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("a", encoding="utf-8-sig", newline="") as csv_file:
            self._csv_file = csv_file
            self._csv_writer = csv.DictWriter(
                csv_file,
                fieldnames=["type", "timestamp", "offset_seconds", "nickname", "content", "raw_json"],
            )
            if csv_file.tell() == 0:
                self._csv_writer.writeheader()

            while not stop():
                client = client_factory(
                    nid_aut=self.tokens.get("NID_AUT", ""),
                    nid_ses=self.tokens.get("NID_SES", ""),
                    auto_reconnect=False,
                    poll_interval=10.0,
                )
                try:
                    async with client as chat:
                        for attr, event_type in [
                            ("on_chat", "chat"),
                            ("on_donation", "donation"),
                            ("on_subscription", "subscription"),
                            ("on_notice", "notice"),
                            ("on_system", "system"),
                        ]:
                            self._bind_handler(chat, attr, event_type)

                        await chat.connect(self.channel_id)
                        logger.info("Chat capture connected for %s", self.channel_id)
                        await chat.run_forever()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if stop():
                        break
                    logger.warning("Chat capture error for %s: %s", self.channel_id, exc)
                else:
                    if not stop():
                        logger.warning("Chat capture disconnected for %s; reconnecting", self.channel_id)

                await self._sleep_until_stop(stop, 5.0)

            logger.info("Chat capture stopped for %s", self.channel_id)
