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
