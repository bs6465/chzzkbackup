from pathlib import Path

from app.chat_csv_migrate import STANDARD_CSV_FIELDS, convert_chat_csv, convert_chat_csv_tree


def test_convert_chat_csv_removes_raw_json_and_reorders_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "chat.csv"
    csv_path.write_text(
        "timestamp,type,offset_seconds,nickname,content,raw_json\n"
        "2026-06-09T22:00:00+09:00,chat,1.25,user,hello,{\"x\":1}\n",
        encoding="utf-8-sig",
    )

    result = convert_chat_csv(csv_path)

    assert result == {"converted": True, "rows": 1}
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == ",".join(STANDARD_CSV_FIELDS)
    assert "raw_json" not in lines[0]
    assert lines[1] == "chat,2026-06-09T22:00:00+09:00,1.25,user,hello"


def test_convert_chat_csv_skips_standard_file(tmp_path: Path) -> None:
    csv_path = tmp_path / "chat.csv"
    csv_path.write_text(",".join(STANDARD_CSV_FIELDS) + "\n", encoding="utf-8-sig")

    result = convert_chat_csv(csv_path)

    assert result == {"converted": False, "rows": 0}


def test_convert_chat_csv_tree_converts_recursive_files(tmp_path: Path) -> None:
    chat_dir = tmp_path / "streamer" / "채팅"
    chat_dir.mkdir(parents=True)
    old_csv = chat_dir / "old.csv"
    new_csv = chat_dir / "new.csv"
    old_csv.write_text("type,timestamp,offset_seconds,nickname,content,raw_json\n", encoding="utf-8-sig")
    new_csv.write_text(",".join(STANDARD_CSV_FIELDS) + "\n", encoding="utf-8-sig")

    result = convert_chat_csv_tree(tmp_path)

    assert result == {"files": 2, "converted": 1, "rows": 0}
