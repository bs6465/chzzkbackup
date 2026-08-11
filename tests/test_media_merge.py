from datetime import datetime, timezone

from app.media_merge import merge_chat_rows, shifted_bookmark_values
from app.media_prepend import build_prefix_chat_rows, infer_target_timeline_start


def test_merge_chat_rows_clamps_first_boundary_and_offsets_second():
    rows = merge_chat_rows(
        [
            {"timestamp": "a", "offset_seconds": 10, "content": "first"},
            {"timestamp": "b", "offset_seconds": 105, "content": "boundary"},
        ],
        [{"timestamp": "c", "offset_seconds": 2.5, "content": "second"}],
        100,
    )
    assert [row["offset_seconds"] for row in rows] == [10, 100, 102.5]
    assert [row["content"] for row in rows] == ["first", "boundary", "second"]


def test_shifted_bookmarks_preserve_displayed_time_across_sources():
    start, end = shifted_bookmark_values(
        {"offset_seconds": 20, "end_offset_seconds": 30},
        timeline_delta=100,
        source_shift=2.5,
        target_shift=-1.5,
        source_duration=200,
    )
    assert start == 124
    assert end == 134


def test_external_chat_alignment_and_prefix_crop():
    external = [
        {
            "playback_seconds": float(index + 5),
            "nickname": f"user-{index}",
            "user_id": f"id-{index}",
            "content": f"message-{index}",
        }
        for index in range(70)
    ]
    existing = [
        {
            "offset_seconds": float(index),
            "nickname": f"user-{index + 35}",
            "content": f"message-{index + 35}",
        }
        for index in range(30)
    ]
    target_start, matched = infer_target_timeline_start(external, existing)
    assert target_start == 40
    assert matched == 30

    rows = build_prefix_chat_rows(
        external,
        prefix_timeline_start=5,
        target_timeline_start=40,
        merged_started_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    assert len(rows) == 35
    assert rows[0]["offset_seconds"] == 0
    assert rows[-1]["offset_seconds"] == 34
