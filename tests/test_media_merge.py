from app.media_merge import merge_chat_rows, shifted_bookmark_values


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
