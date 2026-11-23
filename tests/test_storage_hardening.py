"""Security hardening (storage/helpers): private file modes, sanitized watch-out files, LIKE escaping,
audio symlink refusal."""
import os
import sqlite3
import stat

import pytest

import audio_store
import message_store
import session_review
import usage_index
import watch_scripts


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_new_usage_index_db_and_wal_are_owner_only(tmp_path):
    """usage_index_v2.db used to be created 0644 (umask); it must now be 0600 incl. WAL/SHM."""
    db = tmp_path / "u.db"
    conn = usage_index._connect(db)
    conn.execute("INSERT INTO usage_hourly VALUES ('s','h',1)")
    conn.commit()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(str(db) + suffix):
            assert _mode(str(db) + suffix) == 0o600
    conn.close()


def test_message_store_db_files_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "b.db")
    message_store.init_db()
    for suffix in ("", "-wal", "-shm"):
        p = str(tmp_path / "b.db") + suffix
        if os.path.exists(p):
            assert _mode(p) == 0o600


def test_watch_output_file_is_sanitized_and_private(tmp_path, monkeypatch):
    """The raw output file whose path is handed to the model must not contain secrets."""
    monkeypatch.setattr(watch_scripts, "OUT_ROOT", tmp_path / "out")
    p = watch_scripts.write_output(7, {"rc": 0, "stdout": "password=hunter2 ok", "stderr": "token: abc123"})
    text = p.read_text()
    assert "hunter2" not in text and "abc123" not in text and "ok" in text
    assert _mode(p) == 0o600 and _mode(tmp_path / "out") == 0o700 and _mode(p.parent) == 0o700


def test_session_review_like_wildcards_in_repo_path_are_literal(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, created_at TEXT)")
    c.executemany("INSERT INTO sessions VALUES (?,?,?)", [("a", "/r_x/sub", "2025-01-02"), ("b", "/rax/sub", "2025-01-02")])
    c.commit()
    c.close()
    monkeypatch.setattr(session_review, "SESSION_DB", str(db))
    rows = session_review.query_sessions("/r_x", "2025-01-01", "2025-01-03")
    assert [r[0] for r in rows] == ["a"]


def test_audio_open_refuses_symlink(tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "x.webm"
    link.symlink_to(target)
    with pytest.raises(OSError):
        audio_store._open_nofollow(link, os.O_WRONLY | os.O_TRUNC, "wb")
    assert target.read_text() == "keep"


def test_atomic_write_json_is_private_leaves_no_tmp_and_keeps_old_content_on_failure(tmp_path):
    """Side files (session_titles/active_status/pending_transcriptions) are written tmp+fsync+replace, 0600."""
    p = tmp_path / "t.json"
    message_store.atomic_write_json(p, {"a": 1})
    assert _mode(p) == 0o600 and [x.name for x in tmp_path.iterdir()] == ["t.json"]
    with pytest.raises(TypeError):
        message_store.atomic_write_json(p, {"a": object()})
    assert p.read_text().count('"a": 1') == 1 and [x.name for x in tmp_path.iterdir()] == ["t.json"]


def test_duplicate_script_name_across_dirs_is_rejected(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        f = d / "dup.sh"
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    monkeypatch.setattr(watch_scripts, "SCRIPT_DIR", a)
    monkeypatch.setattr(watch_scripts, "PLUGIN_DIR", b)
    monkeypatch.setattr(watch_scripts, "driver_watch_dirs", lambda: [])
    monkeypatch.setattr(watch_scripts, "plugin_driver_watch_dirs", lambda: [])
    with pytest.raises(ValueError, match="several allow-listed"):
        watch_scripts.resolve_script("dup.sh")


def test_plugin_script_in_group_writable_dir_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    plug = home / ".dop-agent" / "watch-plugins"
    plug.mkdir(parents=True)
    f = plug / "p.sh"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    monkeypatch.setattr(watch_scripts.Path, "home", classmethod(lambda cls: home))
    plug.chmod(0o755)
    assert watch_scripts._check_script(plug, "p.sh", "") == f
    plug.chmod(0o775)
    with pytest.raises(ValueError, match="group/world-writable"):
        watch_scripts._check_script(plug, "p.sh", "")
