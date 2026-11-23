"""
Durable voice-recording store (audio_store.py).

Feature: a Web UI/Slack recording is written to
~/.dop-agent/audio/<conversation_id>/<ts>-<hex>.<ext> BEFORE any processing,
with a sidecar json job record (pending/transcribing/transcribed/done/failed),
so a bot restart mid-transcription or an oversized upload never loses the
recording.
These tests pin: save-first on disk with private permissions, safe names,
streamed copy with a size cap, chunked uploads, resume discovery, and a
retention cleanup that never touches unfinished jobs.
"""

from __future__ import annotations

import io
import os
import time

import pytest

import audio_store


def test_save_bytes_writes_file_first_with_private_permissions_and_pending_sidecar():
    path = audio_store.save_bytes("conv1", b"abc", "note.webm", platform="web", user_id="U1")
    assert path.read_bytes() == b"abc"
    assert path.parent == audio_store.ROOT / "conv1"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    meta = audio_store.read(path)
    assert meta["state"] == "pending" and meta["bytes"] == 3 and meta["user_id"] == "U1"


def test_client_controlled_names_never_reach_the_path():
    path = audio_store.save_bytes("conv1", b"x", "../../etc/passwd.exe")
    assert path.suffix == ".webm"
    assert path.resolve().parent == (audio_store.ROOT / "conv1").resolve()
    with pytest.raises(ValueError):
        audio_store.save_bytes("../evil", b"x", "a.webm")


def test_same_second_uploads_do_not_collide():
    paths = {audio_store.save_bytes("conv1", b"x", "a.webm") for _ in range(20)}
    assert len(paths) == 20


def test_copy_fileobj_streams_and_rejects_over_cap_removing_partial():
    ok = audio_store.copy_fileobj("conv1", io.BytesIO(b"x" * 100), "a.m4a", 1000)
    assert ok.suffix == ".m4a" and ok.stat().st_size == 100
    with pytest.raises(audio_store.AudioTooLarge):
        audio_store.copy_fileobj("conv1", io.BytesIO(b"x" * 100), "b.webm", 10)
    assert [p.name for p in (audio_store.ROOT / "conv1").glob("*.webm")] == []
    with pytest.raises(ValueError):
        audio_store.copy_fileobj("conv1", io.BytesIO(b""), "c.webm", 10)


def test_chunked_upload_appends_idempotently_and_finishes_to_pending():
    path = audio_store.begin_chunked("conv1", "v.webm", platform="web")
    assert audio_store.resolve_upload("conv1", path.stem) == path
    assert audio_store.append_chunk(path, 0, b"aa", 100) == 1
    assert audio_store.append_chunk(path, 0, b"aa", 100) == 1  # retried chunk ignored
    assert audio_store.append_chunk(path, 1, b"bb", 100) == 2
    with pytest.raises(ValueError):
        audio_store.append_chunk(path, 5, b"zz", 100)  # gap
    assert audio_store.finish_chunked(path) is True
    assert path.read_bytes() == b"aabb"
    assert audio_store.read(path)["state"] == "pending"
    assert audio_store.resolve_upload("conv1", "../x") is None


def test_abandoned_chunked_upload_is_promoted_to_pending_after_idle():
    path = audio_store.begin_chunked("conv1", "v.webm", platform="web")
    audio_store.append_chunk(path, 0, b"aa", 100)
    assert audio_store.promote_stale() == []  # still fresh
    audio_store.update(path, updated_at=time.time() - 1000)
    assert [p for p, _ in audio_store.promote_stale()] == [path]
    assert audio_store.read(path)["state"] == "pending"


def test_list_unfinished_returns_only_resumable_states():
    a = audio_store.save_bytes("c", b"1", "a.webm")
    b = audio_store.save_bytes("c", b"2", "b.webm")
    d = audio_store.save_bytes("c", b"3", "d.webm")
    audio_store.update(b, state="transcribing")
    audio_store.mark_done(d)
    assert {p for p, _ in audio_store.list_unfinished()} == {a, b}


def test_cleanup_deletes_only_old_done_delivered_jobs():
    old_done = audio_store.save_bytes("c", b"1", "a.webm")
    audio_store.mark_done(old_done)
    audio_store.update(old_done, updated_at=time.time() - 8 * 86400)
    old_pending = audio_store.save_bytes("c", b"2", "b.webm")
    audio_store.update(old_pending, updated_at=time.time() - 30 * 86400)
    old_failed = audio_store.save_bytes("c", b"3", "c.webm")
    audio_store.update(old_failed, state="failed", updated_at=time.time() - 30 * 86400)
    fresh_done = audio_store.save_bytes("c", b"4", "d.webm")
    audio_store.mark_done(fresh_done)

    assert audio_store.cleanup(7) == 1
    assert not old_done.exists() and not audio_store.sidecar_path(old_done).exists()
    assert old_pending.exists() and old_failed.exists() and fresh_done.exists()


def test_interrupted_streamed_upload_with_no_data_is_cleaned_and_partial_data_is_promoted():
    """A one-shot upload cut off by a bot restart is judged by real file size
    (not the sidecar): empty -> removed, partial -> promoted to a normal
    pending job."""
    empty = audio_store.begin_chunked("c", "a.webm", platform="web")
    partial = audio_store.begin_chunked("c", "b.webm", platform="web")
    partial.write_bytes(b"some audio")
    for p in (empty, partial):
        audio_store.update(p, updated_at=time.time() - 1000)

    assert [p for p, _ in audio_store.promote_stale()] == [partial]
    assert not empty.exists() and audio_store.read(partial)["state"] == "pending"


def test_upload_still_being_streamed_by_this_process_is_never_promoted():
    path = audio_store.begin_chunked("c", "a.webm", platform="web")
    path.write_bytes(b"x")
    audio_store.update(path, updated_at=time.time() - 1000)
    audio_store._active_copies.add(str(path))
    try:
        assert audio_store.promote_stale() == []
    finally:
        audio_store._active_copies.discard(str(path))


def test_failed_jobs_are_requeued_once_at_startup_and_on_demand_and_audio_is_kept():
    p = audio_store.save_bytes("c", b"1", "a.webm")
    audio_store.update(p, state="failed", error="boom", attempts=3)
    assert audio_store.retry_failed(startup=True) == [p]
    assert audio_store.read(p)["state"] == "pending" and audio_store.read(p)["attempts"] == 0
    audio_store.update(p, state="failed")
    assert audio_store.retry_failed(startup=True) == []  # only one automatic retry
    assert audio_store.retry_failed(conversation_id="other") == []
    assert audio_store.retry_failed(conversation_id="c") == [p]
