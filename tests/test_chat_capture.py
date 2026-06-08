from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest

from app.chat_capture import ChatCapture


@pytest.mark.asyncio
async def test_chat_capture_reconnects_after_client_returns(tmp_path):
    stopped = {"value": False}
    instances = []

    class FakeChatClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.index = len(instances) + 1
            instances.append(self)
            self.chat_handler = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def on_chat(self, handler):
            self.chat_handler = handler
            return handler

        def on_donation(self, handler):
            return handler

        def on_system(self, handler):
            return handler

        async def connect(self, channel_id):
            self.channel_id = channel_id

        async def run_forever(self):
            await self.chat_handler({"nickname": f"user-{self.index}", "content": f"msg-{self.index}"})
            if self.index == 2:
                stopped["value"] = True

    started_at = datetime(2026, 6, 7, 18, 55, 26, tzinfo=ZoneInfo("Asia/Seoul"))
    capture = ChatCapture(
        "channel-1",
        {"NID_AUT": "aut", "NID_SES": "ses"},
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
        started_at,
        client_factory=FakeChatClient,
    )

    await capture.run(lambda: stopped["value"])

    assert len(instances) == 2
    assert [instance.kwargs["auto_reconnect"] for instance in instances] == [False, False]
    assert (tmp_path / "chat.jsonl").read_text(encoding="utf-8").count("\n") == 2


@pytest.mark.asyncio
async def test_chat_capture_handles_none_profile(tmp_path):
    started_at = datetime(2026, 6, 7, 18, 55, 26, tzinfo=ZoneInfo("Asia/Seoul"))
    capture = ChatCapture(
        "channel-1",
        {"NID_AUT": "aut", "NID_SES": "ses"},
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
        started_at,
    )

    await capture._write_event("chat", {"profile": None, "content": "hello"})

    row = json.loads((tmp_path / "chat.jsonl").read_text(encoding="utf-8"))
    assert row["nickname"] == ""
    assert row["content"] == "hello"
