import sqlite3

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


def test_platform_columns_backfill_existing_database(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE channels (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE recording_sessions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id TEXT NOT NULL,
          channel_name TEXT NOT NULL,
          live_id TEXT,
          live_title TEXT,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          status TEXT NOT NULL,
          temp_path TEXT,
          source_path TEXT,
          final_path TEXT,
          error TEXT
        );
        CREATE TABLE encode_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id INTEGER NOT NULL,
          source_path TEXT NOT NULL,
          final_path TEXT NOT NULL,
          status TEXT NOT NULL,
          error TEXT,
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT
        );
        CREATE TABLE app_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          level TEXT NOT NULL,
          message TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        INSERT INTO channels (id, name, active, created_at, updated_at)
        VALUES ('abc123', 'Streamer', 1, 'now', 'now');
        INSERT INTO recording_sessions (channel_id, channel_name, live_title, started_at, status)
        VALUES ('abc123', 'Streamer', 'Title', 'now', 'completed');
        """
    )
    conn.commit()
    conn.close()

    database = Database(db_path)

    channel = database.get_channel("abc123")
    session = database.recent_sessions(1)[0]
    assert channel["platform"] == "chzzk"
    assert channel["display_id"] == "abc123"
    assert session["platform"] == "chzzk"
    assert session["channel_display_id"] == "abc123"


def test_twitcasting_channel_crud_stores_display_id(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel(
        "twitcasting:alice",
        "Alice",
        platform="twitcasting",
        display_id="alice",
    )

    channel = database.get_channel("twitcasting:alice")

    assert channel["platform"] == "twitcasting"
    assert channel["display_id"] == "alice"


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


def test_recover_interrupted_sessions_marks_recording_failed(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
    )

    assert database.recover_interrupted_sessions() == {"queued": 0, "failed": 1}

    session = database.query_one("SELECT * FROM recording_sessions WHERE id = ?", (session_id,))
    assert session["status"] == "failed"
    assert session["ended_at"]
    assert "restart" in session["error"]


def test_finalize_session_chat_files_moves_temp_files(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    jsonl_temp = tmp_path / "temp.chat.jsonl.part"
    csv_temp = tmp_path / "temp.chat.csv.part"
    jsonl_final = tmp_path / "final" / "chat.jsonl"
    csv_final = tmp_path / "final" / "chat.csv"
    jsonl_temp.write_text('{"content":"hello"}\n', encoding="utf-8")
    csv_temp.write_text("type,content\nchat,hello\n", encoding="utf-8")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        jsonl_final,
        csv_final,
        jsonl_temp,
        csv_temp,
    )

    moved = database.finalize_session_chat_files(session_id)

    assert moved == {"jsonl": jsonl_final, "csv": csv_final}
    assert jsonl_final.read_text(encoding="utf-8") == '{"content":"hello"}\n'
    assert csv_final.read_text(encoding="utf-8") == "type,content\nchat,hello\n"
    assert not jsonl_temp.exists()
    assert not csv_temp.exists()


def test_finalize_session_chat_files_uses_unique_destination(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    jsonl_temp = tmp_path / "temp.chat.jsonl.part"
    csv_temp = tmp_path / "temp.chat.csv.part"
    jsonl_final = tmp_path / "final" / "chat.jsonl"
    csv_final = tmp_path / "final" / "chat.csv"
    jsonl_final.parent.mkdir(parents=True)
    jsonl_final.write_text("existing\n", encoding="utf-8")
    csv_final.write_text("existing\n", encoding="utf-8")
    jsonl_temp.write_text("new\n", encoding="utf-8")
    csv_temp.write_text("type,content\nchat,new\n", encoding="utf-8")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        jsonl_final,
        csv_final,
        jsonl_temp,
        csv_temp,
    )

    moved = database.finalize_session_chat_files(session_id)
    session = database.query_one("SELECT * FROM recording_sessions WHERE id = ?", (session_id,))

    assert moved["jsonl"] == tmp_path / "final" / "chat_1.jsonl"
    assert moved["csv"] == tmp_path / "final" / "chat_1.csv"
    assert session["chat_jsonl_path"] == str(tmp_path / "final" / "chat_1.jsonl")
    assert session["chat_csv_path"] == str(tmp_path / "final" / "chat_1.csv")


def test_finalize_session_chat_files_discards_empty_chat_files(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    jsonl_temp = tmp_path / "temp.chat.jsonl.part"
    csv_temp = tmp_path / "temp.chat.csv.part"
    jsonl_temp.write_text("", encoding="utf-8")
    csv_temp.write_text("type,timestamp,offset_seconds,nickname,content,raw_json\n", encoding="utf-8")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "final" / "chat.jsonl",
        tmp_path / "final" / "chat.csv",
        jsonl_temp,
        csv_temp,
    )

    moved = database.finalize_session_chat_files(session_id)

    assert moved == {"jsonl": None, "csv": None}
    assert not jsonl_temp.exists()
    assert not csv_temp.exists()
    assert not (tmp_path / "final" / "chat.jsonl").exists()
    assert not (tmp_path / "final" / "chat.csv").exists()


def test_recover_interrupted_sessions_queues_existing_temp_file(tmp_path, monkeypatch):
    from app import db as db_module

    database = Database(tmp_path / "test.sqlite3")
    monkeypatch.setattr(db_module.config, "FINAL_ROOT", tmp_path / "final")
    temp_path = tmp_path / "recording.ts.part"
    temp_path.write_text("video")
    database.upsert_channel("abc123", "Streamer")
    chat_jsonl_temp = tmp_path / "chat.jsonl.part"
    chat_csv_temp = tmp_path / "chat.csv.part"
    chat_jsonl_temp.write_text('{"content":"chat"}\n', encoding="utf-8")
    chat_csv_temp.write_text("type,content\nchat,chat\n", encoding="utf-8")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        temp_path,
        tmp_path / "final" / "Streamer" / "채팅" / "chat.jsonl",
        tmp_path / "final" / "Streamer" / "채팅" / "chat.csv",
        chat_jsonl_temp,
        chat_csv_temp,
    )

    assert database.recover_interrupted_sessions() == {"queued": 1, "failed": 0}

    session = database.query_one("SELECT * FROM recording_sessions WHERE id = ?", (session_id,))
    job = database.query_one("SELECT * FROM encode_jobs WHERE session_id = ?", (session_id,))
    assert session["status"] == "queued"
    assert session["source_path"] == str(tmp_path / "recording.ts")
    assert job["status"] == "queued"
    assert job["source_path"] == str(tmp_path / "recording.ts")
    assert job["final_path"] == str(tmp_path / "final" / "Streamer" / "recording.mp4")
    assert (tmp_path / "final" / "Streamer" / "채팅" / "chat.jsonl").exists()
    assert (tmp_path / "final" / "Streamer" / "채팅" / "chat.csv").exists()


def test_recover_interrupted_encode_jobs_requeues_when_source_exists(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    source_path = tmp_path / "source.ts"
    final_path = tmp_path / "final.mp4"
    source_path.write_text("video")
    database.upsert_channel("abc123", "Streamer")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
    )
    database.finish_session(session_id, source_path, "queued")
    job_id = database.add_encode_job(session_id, source_path, final_path)
    database.update_encode_job(job_id, "running")
    database.update_session_status(session_id, "encoding")

    assert database.recover_interrupted_encode_jobs() == {"queued": 1, "completed": 0, "failed": 0}

    job = database.query_one("SELECT * FROM encode_jobs WHERE id = ?", (job_id,))
    session = database.query_one("SELECT * FROM recording_sessions WHERE id = ?", (session_id,))
    assert job["status"] == "queued"
    assert job["started_at"] is None
    assert job["progress_percent"] == 0
    assert job["duration_seconds"] is None
    assert session["status"] == "queued"


def test_recover_interrupted_encode_jobs_completes_when_final_exists(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    source_path = tmp_path / "source.ts"
    final_path = tmp_path / "final.mp4"
    final_path.write_text("encoded")
    database.upsert_channel("abc123", "Streamer")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
    )
    database.finish_session(session_id, source_path, "queued")
    job_id = database.add_encode_job(session_id, source_path, final_path)
    database.update_encode_job(job_id, "running")
    database.update_session_status(session_id, "encoding")

    assert database.recover_interrupted_encode_jobs() == {"queued": 0, "completed": 1, "failed": 0}

    job = database.query_one("SELECT * FROM encode_jobs WHERE id = ?", (job_id,))
    session = database.query_one("SELECT * FROM recording_sessions WHERE id = ?", (session_id,))
    assert job["status"] == "completed"
    assert job["progress_percent"] == 100
    assert session["status"] == "completed"
    assert session["final_path"] == str(final_path)


def test_update_encode_progress(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    source_path = tmp_path / "source.ts"
    final_path = tmp_path / "final.mp4"
    database.upsert_channel("abc123", "Streamer")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Title",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "chat.jsonl",
        tmp_path / "chat.csv",
    )
    database.finish_session(session_id, source_path, "queued")
    job_id = database.add_encode_job(session_id, source_path, final_path)

    database.update_encode_job(job_id, "running")
    database.update_encode_progress(
        job_id,
        duration_seconds=100,
        encoded_seconds=25,
        progress_percent=25,
        speed="2x",
        eta_seconds=37.5,
    )

    job = database.query_one("SELECT * FROM encode_jobs WHERE id = ?", (job_id,))
    assert job["duration_seconds"] == 100
    assert job["encoded_seconds"] == 25
    assert job["progress_percent"] == 25
    assert job["speed"] == "2x"
    assert job["eta_seconds"] == 37.5
    assert job["progress_updated_at"]


def test_rename_recording_session_updates_recording_final_paths_without_temp_move(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    temp_path = tmp_path / "temp" / "[260607 10-00-00] Streamer - Old.ts.part"
    chat_jsonl_temp = tmp_path / "temp" / "[260607 10-00-00] Streamer - Old.chat.jsonl.part"
    chat_csv_temp = tmp_path / "temp" / "[260607 10-00-00] Streamer - Old.chat.csv.part"
    final_path = tmp_path / "final" / "Streamer" / "[260607 10-00-00] Streamer - Old.mp4"
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Old",
        "2026-06-07T10:00:00+09:00",
        temp_path,
        tmp_path / "final" / "Streamer" / "채팅" / "[260607 10-00-00] Streamer - Old.jsonl",
        tmp_path / "final" / "Streamer" / "채팅" / "[260607 10-00-00] Streamer - Old.csv",
        chat_jsonl_temp,
        chat_csv_temp,
        final_path,
    )

    renamed = database.rename_session_title(session_id, "New / Title")

    assert renamed["live_title"] == "New Title"
    assert renamed["temp_path"] == str(temp_path)
    assert renamed["chat_jsonl_temp_path"] == str(chat_jsonl_temp)
    assert renamed["final_path"].endswith("[260607 10-00-00] Streamer - New Title.mp4")
    assert renamed["chat_jsonl_path"].endswith("[260607 10-00-00] Streamer - New Title.jsonl")
    assert renamed["chat_csv_path"].endswith("[260607 10-00-00] Streamer - New Title.csv")


def test_rename_queued_session_updates_encode_job_final_path(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    source_path = tmp_path / "source.ts"
    final_path = tmp_path / "final" / "Streamer" / "[260607 10-00-00] Streamer - Old.mp4"
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Old",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "final" / "Streamer" / "채팅" / "[260607 10-00-00] Streamer - Old.jsonl",
        tmp_path / "final" / "Streamer" / "채팅" / "[260607 10-00-00] Streamer - Old.csv",
        final_path=final_path,
    )
    database.finish_session(session_id, source_path, "queued")
    job_id = database.add_encode_job(session_id, source_path, final_path)

    database.rename_session_title(session_id, "New")

    job = database.query_one("SELECT * FROM encode_jobs WHERE id = ?", (job_id,))
    assert job["final_path"].endswith("[260607 10-00-00] Streamer - New.mp4")


def test_rename_completed_session_moves_video_and_chat_with_shared_suffix(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    video_dir = tmp_path / "final" / "Streamer"
    chat_dir = video_dir / "채팅"
    video_dir.mkdir(parents=True)
    chat_dir.mkdir()
    old_mp4 = video_dir / "[260607 10-00-00] Streamer - Old.mp4"
    old_jsonl = chat_dir / "[260607 10-00-00] Streamer - Old.jsonl"
    old_csv = chat_dir / "[260607 10-00-00] Streamer - Old.csv"
    old_mp4.write_text("video", encoding="utf-8")
    old_jsonl.write_text("jsonl", encoding="utf-8")
    old_csv.write_text("csv", encoding="utf-8")
    (video_dir / "[260607 10-00-00] Streamer - New.mp4").write_text("collision", encoding="utf-8")
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Old",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        old_jsonl,
        old_csv,
        final_path=old_mp4,
    )
    database.update_session_status(session_id, "completed", final_path=old_mp4)

    renamed = database.rename_session_title(session_id, "New")

    assert not old_mp4.exists()
    assert not old_jsonl.exists()
    assert not old_csv.exists()
    assert renamed["final_path"].endswith("[260607 10-00-00] Streamer - New_1.mp4")
    assert renamed["chat_jsonl_path"].endswith("[260607 10-00-00] Streamer - New_1.jsonl")
    assert renamed["chat_csv_path"].endswith("[260607 10-00-00] Streamer - New_1.csv")
    assert (video_dir / "[260607 10-00-00] Streamer - New_1.mp4").read_text(encoding="utf-8") == "video"
    assert (chat_dir / "[260607 10-00-00] Streamer - New_1.jsonl").read_text(encoding="utf-8") == "jsonl"


def test_finalize_encode_output_moves_running_job_to_renamed_path(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_channel("abc123", "Streamer")
    old_output = tmp_path / "final" / "Streamer" / "[260607 10-00-00] Streamer - Old.mp4"
    old_output.parent.mkdir(parents=True)
    old_output.write_text("encoded", encoding="utf-8")
    source_path = tmp_path / "source.ts"
    session_id = database.create_session(
        "abc123",
        "Streamer",
        "live-1",
        "Old",
        "2026-06-07T10:00:00+09:00",
        tmp_path / "recording.ts.part",
        tmp_path / "final" / "Streamer" / "채팅" / "[260607 10-00-00] Streamer - Old.jsonl",
        tmp_path / "final" / "Streamer" / "채팅" / "[260607 10-00-00] Streamer - Old.csv",
        final_path=old_output,
    )
    database.finish_session(session_id, source_path, "queued")
    job_id = database.add_encode_job(session_id, source_path, old_output)
    database.update_encode_job(job_id, "running")
    database.update_session_status(session_id, "encoding")
    database.rename_session_title(session_id, "New")

    final_output = database.finalize_encode_output(job_id, old_output)

    assert not old_output.exists()
    assert final_output.name == "[260607 10-00-00] Streamer - New.mp4"
    assert final_output.read_text(encoding="utf-8") == "encoded"
