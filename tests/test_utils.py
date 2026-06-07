from pathlib import Path

from app import config
from app.utils import (
    disk_status,
    format_duration,
    kst_display,
    mask_secret,
    sanitize_name,
    shorten_filename,
    unique_path,
)


def test_sanitize_name_removes_unsafe_chars():
    assert sanitize_name('a/b:c*?"<>|') == "abc"
    assert sanitize_name("   ") == "untitled"


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("abcdef") == "******"
    assert mask_secret("abcd1234wxyz") == "abcd****wxyz"


def test_shorten_filename_keeps_suffix_under_limit():
    name = shorten_filename(("가" * 200) + ".mp4")
    assert name.endswith(".mp4")
    assert len(name.encode("utf-8")) <= 255


def test_unique_path(tmp_path: Path):
    first = tmp_path / "file.mp4"
    first.write_text("x")
    assert unique_path(first).name == "file_1.mp4"


def test_disk_status(tmp_path: Path):
    status = disk_status(tmp_path)
    assert status["path"] == str(tmp_path)
    assert status["free"] > 0


def test_kst_display_converts_utc_iso_string():
    assert kst_display("2026-06-06T20:01:10.313427+00:00") == "2026-06-07T05:01:10+09:00"


def test_format_duration():
    assert format_duration(62.4) == "1:02"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(None) == "-"
