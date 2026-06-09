import csv
import json

from app.chat_merge import merge_chat_fragments


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_merge_chat_fragments_sorts_jsonl_and_deletes_originals(tmp_path):
    first = tmp_path / "[260607 23-30-01] 아리사 - 썬더일레븐 2일차.jsonl"
    second = tmp_path / "[260607 23-30-07] 아리사 - 썬더일레븐 2일차.jsonl"
    write_jsonl(first, [{"timestamp": "2026-06-07T23:30:02+09:00", "offset_seconds": 2, "content": "b"}])
    write_jsonl(second, [{"timestamp": "2026-06-07T23:30:01+09:00", "offset_seconds": 1, "content": "a"}])

    result = merge_chat_fragments(tmp_path, delete_originals=True)

    assert result["groups"] == 1
    assert result["jsonl"] == 1
    assert result["deleted"] == 1
    assert first.exists()
    assert not second.exists()
    rows = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
    assert [row["content"] for row in rows] == ["a", "b"]


def test_merge_chat_fragments_keeps_single_csv_header(tmp_path):
    first = tmp_path / "[260607 23-30-01] 아리사 - 썬더일레븐 2일차.csv"
    second = tmp_path / "[260607 23-30-07] 아리사 - 썬더일레븐 2일차.csv"
    for path, content in [
        (first, "type,timestamp,offset_seconds,content\nchat,2026-06-07T23:30:02+09:00,2,b\n"),
        (second, "type,timestamp,offset_seconds,content\nchat,2026-06-07T23:30:01+09:00,1,a\n"),
    ]:
        path.write_text(content, encoding="utf-8")

    result = merge_chat_fragments(tmp_path, delete_originals=True)

    assert result["groups"] == 1
    assert result["csv"] == 1
    assert not second.exists()
    with first.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["content"] for row in rows] == ["a", "b"]
    assert list(rows[0]) == ["type", "timestamp", "offset_seconds", "nickname", "content"]
