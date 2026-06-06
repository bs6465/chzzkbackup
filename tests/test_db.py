from app.db import Database


def test_channel_crud(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    channel = database.get_channel("abc123")
    assert channel is not None
    assert channel["name"] == "Streamer"

    database.rename_channel("abc123", "NewName")
    assert database.get_channel("abc123")["name"] == "NewName"

    database.set_channel_active("abc123", False)
    assert database.get_channel("abc123")["active"] == 0

    database.delete_channel("abc123")
    assert database.get_channel("abc123") is None


def test_tokens(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.set_tokens("ses;bad", "aut\nbad")
    assert database.get_tokens() == {"NID_SES": "sesbad", "NID_AUT": "autbad"}


def test_log_cleanup_keeps_latest_count(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    for index in range(5):
        database.add_log("info", f"log-{index}")

    database.cleanup_old_logs(days=30, max_rows=3)

    rows = database.recent_logs(limit=10)
    assert [row["message"] for row in rows] == ["log-4", "log-3", "log-2"]
