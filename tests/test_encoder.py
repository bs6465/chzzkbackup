from pathlib import Path

from app.encoder import (
    build_x264_mp4_command,
    calculate_progress,
    parse_ffmpeg_seconds,
    parse_speed_factor,
)


def test_build_x264_mp4_command():
    cmd = build_x264_mp4_command(Path("/tmp/in.ts"), Path("/tmp/out.mp4"))
    assert "-c:v" in cmd
    assert "libx264" in cmd
    assert "-preset" in cmd
    assert "veryfast" in cmd
    assert "-crf" in cmd
    assert "28" in cmd
    assert "-progress" in cmd
    assert "pipe:2" in cmd
    assert "-nostats" in cmd
    assert cmd[-1] == "/tmp/out.mp4"


def test_build_x264_mp4_command_supports_concat_manifest():
    cmd = build_x264_mp4_command(Path("/tmp/in.concat"), Path("/tmp/out.mp4"))
    assert cmd[cmd.index("-f") + 1] == "concat"
    assert "-safe" in cmd


def test_parse_ffmpeg_seconds():
    assert parse_ffmpeg_seconds("00:01:02.500000") == 62.5
    assert parse_ffmpeg_seconds("2500000") == 2.5
    assert parse_ffmpeg_seconds("not-time") is None


def test_parse_speed_factor():
    assert parse_speed_factor("2.5x") == 2.5
    assert parse_speed_factor("0x") is None
    assert parse_speed_factor("N/A") is None


def test_calculate_progress_and_eta():
    progress, eta = calculate_progress(100, 25, "2x")
    assert progress == 25
    assert eta == 37.5
