from datetime import datetime
from zoneinfo import ZoneInfo

from app.recorder import recording_name


def test_recording_name_uses_bracketed_short_date_format():
    started = datetime(2026, 6, 4, 0, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert (
        recording_name(started, "한도이 Doi", "제목", ".mp4")
        == "[260604 00-00-00] 한도이 Doi - 제목.mp4"
    )
