from pathlib import Path

from app.encoder import build_x264_mp4_command


def test_build_x264_mp4_command():
    cmd = build_x264_mp4_command(Path("/tmp/in.ts"), Path("/tmp/out.mp4"))
    assert "-c:v" in cmd
    assert "libx264" in cmd
    assert "-preset" in cmd
    assert "veryfast" in cmd
    assert "-crf" in cmd
    assert "28" in cmd
    assert cmd[-1] == "/tmp/out.mp4"
