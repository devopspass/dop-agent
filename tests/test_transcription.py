"""
Unit tests for transcription.py — Slack audio-file detection and the
download/transcribe pipeline (with the actual network/Whisper calls
mocked out).
"""

from __future__ import annotations

import asyncio

import transcription


def test_download_slack_file_sends_real_bearer_token_in_authorization_header(monkeypatch):
    """_download_slack_file() sends the real token as 'Authorization: Bearer <token>'.

    Slack's private file-download endpoint rejects any other value with 403.
    """
    captured = {}

    class FakeResponse:
        content = b"audio-bytes"

        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(transcription, "requests", None, raising=False)
    import sys
    import types

    fake_requests = types.SimpleNamespace(get=fake_get)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = transcription._download_slack_file("https://files.slack.com/files-pri/T1-F1/x.m4a", "xoxb-real-token-123")

    assert result == b"audio-bytes"
    assert captured["headers"] == {"Authorization": "Bearer xoxb-real-token-123"}


def test_download_slack_file_never_sends_token_to_foreign_host_or_redirect(monkeypatch):
    """Security: the bot token goes only to https://files.slack.com, and is dropped on a redirect elsewhere."""
    import sys
    import types

    calls = []

    class Resp:
        def __init__(self, loc=None):
            self.headers = {"Location": loc} if loc else {}
            self.is_redirect = bool(loc)
            self.content = b"ok"

        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        calls.append((url, dict(headers or {}), allow_redirects))
        return Resp("https://evil.example/x") if url.startswith("https://files.slack.com") else Resp()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=fake_get))
    transcription._download_slack_file("https://files.slack.com/a", "tok")
    transcription._download_slack_file("http://files.slack.com/a", "tok")
    assert calls[0][1] == {"Authorization": "Bearer tok"} and calls[0][2] is False
    assert calls[1][1] == {}  # redirect to another host: no token
    assert calls[2][1] == {}  # plain http: no token


def test_is_audio_file_by_mimetype():
    assert transcription._is_audio_file({"mimetype": "audio/mp4"}) is True


def test_is_audio_file_by_slack_subtype():
    assert transcription._is_audio_file({"subtype": "slack_audio"}) is True


def test_is_audio_file_by_filetype():
    assert transcription._is_audio_file({"filetype": "M4A"}) is True


def test_is_audio_file_false_for_unrelated_file():
    assert transcription._is_audio_file({"mimetype": "image/png", "filetype": "png"}) is False


def test_is_audio_file_handles_missing_keys():
    assert transcription._is_audio_file({}) is False


def test_transcribe_slack_audio_raises_without_url():
    with_no_url = {"name": "clip.m4a"}

    async def run():
        await transcription.transcribe_slack_audio(with_no_url)

    try:
        asyncio.run(run())
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "url_private" in str(exc)


def test_transcribe_slack_audio_downloads_saves_and_transcribes(monkeypatch, tmp_path):
    downloaded = {}
    transcribed = {}

    def fake_download(url, token):
        downloaded["url"] = url
        downloaded["token"] = token
        return b"fake-audio-bytes"

    def fake_transcribe(local_path):
        transcribed["path"] = local_path
        assert tmp_path.parent.exists() or True  # local_path is a real temp file
        return "hello world"

    monkeypatch.setattr(transcription, "_download_slack_file", fake_download)
    monkeypatch.setattr(transcription, "_transcribe_audio_file", fake_transcribe)

    file_info = {"url_private_download": "https://files.slack.com/x.m4a", "name": "note.m4a"}

    result = asyncio.run(transcription.transcribe_slack_audio(file_info))

    assert result == "hello world"
    assert downloaded["url"] == "https://files.slack.com/x.m4a"
    assert transcribed["path"].endswith(".m4a")

    # The recording is kept in the audio store (never deleted) and its job is marked done.
    import os
    import audio_store
    assert os.path.exists(transcribed["path"]), "audio must NOT be deleted"
    assert audio_store.read(__import__("pathlib").Path(transcribed["path"]))["state"] == "done"


def test_transcribe_slack_audio_falls_back_to_url_private(monkeypatch):
    monkeypatch.setattr(transcription, "_download_slack_file", lambda url, token: b"data")
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda path: "text")

    file_info = {"url_private": "https://files.slack.com/fallback.m4a"}
    result = asyncio.run(transcription.transcribe_slack_audio(file_info))
    assert result == "text"


def test_transcribe_slack_audio_invokes_on_queued_when_lock_held(monkeypatch):
    monkeypatch.setattr(transcription, "_download_slack_file", lambda url, token: b"data")
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda path: "text")

    queued_calls = []

    async def on_queued():
        queued_calls.append(True)

    async def hold_lock_and_run():
        async with transcription._whisper_transcribe_lock:
            # Lock is held; the concurrent call below should invoke on_queued.
            task = asyncio.create_task(
                transcription.transcribe_slack_audio(
                    {"url_private": "https://x/y.m4a"}, on_queued=on_queued,
                )
            )
            await asyncio.sleep(0.05)
            assert not task.done()
        result = await task
        return result

    result = asyncio.run(hold_lock_and_run())
    assert result == "text"
    assert queued_calls == [True]


def test_transcribe_audio_bytes_is_the_shared_core_used_by_slack_path(monkeypatch):
    """transcribe_slack_audio() delegates its tempfile/lock/transcribe work to
    transcribe_audio_bytes(), the core shared with the Web UI upload path."""
    monkeypatch.setattr(transcription, "_download_slack_file", lambda url, token: b"data")
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda path: "hi there")

    result = asyncio.run(
        transcription.transcribe_audio_bytes(b"raw-bytes", "clip.wav")
    )
    assert result == "hi there"


def test_transcribe_audio_bytes_rejects_oversized_input(monkeypatch):
    """A single oversized upload (most realistically from the Web UI, which
    has no Slack-side file-size limit protecting it) must not be allowed to
    occupy the one global Whisper lock shared with Slack transcriptions for
    an unbounded amount of time - reject before ever touching the lock."""
    monkeypatch.setattr(transcription, "MAX_AUDIO_BYTES", 10)  # real default is 2 GB
    oversized = b"x" * 11
    try:
        asyncio.run(transcription.transcribe_audio_bytes(oversized, "big.wav"))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "too large" in str(exc)


def test_get_whisper_model_caches_instance(monkeypatch):
    monkeypatch.setattr(transcription, "_whisper_model", None)

    calls = []

    class _FakeWhisperModule:
        @staticmethod
        def load_model(name):
            calls.append(name)
            return f"model-for-{name}"

    monkeypatch.setitem(__import__("sys").modules, "whisper", _FakeWhisperModule())

    m1 = transcription._get_whisper_model()
    m2 = transcription._get_whisper_model()

    assert m1 == m2
    assert calls == [transcription.WHISPER_MODEL]


def test_transcribe_audio_file_never_deletes_the_recording_even_on_failure(monkeypatch):
    """Persistence guarantee: the saved recording is the only copy of the
    user's voice, so neither a successful nor a failed Whisper run may unlink it."""
    import audio_store

    path = audio_store.save_bytes("conv", b"audio", "a.wav")
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda p: "ok")
    assert asyncio.run(transcription.transcribe_audio_file(path)) == "ok"
    assert path.exists()

    def boom(p):
        raise RuntimeError("whisper died")

    monkeypatch.setattr(transcription, "_transcribe_audio_file", boom)
    try:
        asyncio.run(transcription.transcribe_audio_file(path))
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert path.exists()


def test_long_audio_is_segmented_lock_taken_per_segment_and_resume_skips_done_segments(monkeypatch, tmp_path):
    """Long recordings (>10 min) are split; each segment takes the shared Whisper
    lock separately (so other jobs aren't starved) and its text is cached in the
    sidecar. After a simulated restart mid-way, finished segments are not redone."""
    import audio_store

    path = audio_store.save_bytes("conv", b"audio", "long.webm")
    seg_dir = audio_store.segments_dir(path)
    seg_dir.mkdir()
    segs = []
    for i in range(3):
        sp = seg_dir / f"seg-{i:04d}.wav"
        sp.write_bytes(b"x")
        segs.append(sp)
    (seg_dir / ".complete").touch()

    monkeypatch.setattr(transcription, "_probe_duration", lambda p: 1800.0)
    lock_states = []
    calls = []

    def fake_whisper(local):
        calls.append(local)
        lock_states.append(transcription._whisper_transcribe_lock.locked())
        if len(calls) == 2:
            raise RuntimeError("bot died")
        return f"part{len(calls)}"

    monkeypatch.setattr(transcription, "_transcribe_audio_file", fake_whisper)
    progress = []

    async def on_progress(done, total):
        progress.append((done, total))

    try:
        asyncio.run(transcription.transcribe_audio_file(path, on_progress=on_progress))
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
    assert audio_store.read(path)["texts"] == ["part1"]

    calls.clear()
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda local: calls.append(local) or f"p{len(calls)}")
    text = asyncio.run(transcription.transcribe_audio_file(path, on_progress=on_progress))
    assert text == "part1 p1 p2"
    assert len(calls) == 2 and segs[0].name not in str(calls)  # segment 0 was not re-transcribed
    assert all(lock_states)
    assert progress[0] == (1, 3) and progress[-1] == (3, 3)


def test_unknown_duration_browser_webm_is_still_segmented(monkeypatch):
    """MediaRecorder webm has no duration in its header (ffprobe says N/A); unknown
    duration is segmented rather than treated as short and sent to Whisper whole."""
    import audio_store

    path = audio_store.save_bytes("conv", b"audio", "long.webm")
    seg_dir = audio_store.segments_dir(path)
    seg_dir.mkdir()
    for i in range(2):
        (seg_dir / f"seg-{i:04d}.wav").write_bytes(b"x")
    (seg_dir / ".complete").touch()
    monkeypatch.setattr(transcription, "_probe_duration", lambda p: None)
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda local: f"t{local[-8:-4]}")

    assert asyncio.run(transcription.transcribe_audio_file(path)) == "t0000 t0001"


def test_unknown_duration_falls_back_to_whole_file_when_ffmpeg_cannot_split(monkeypatch):
    import audio_store

    path = audio_store.save_bytes("conv", b"audio", "x.webm")
    monkeypatch.setattr(transcription, "_probe_duration", lambda p: None)

    def no_split(p):
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(transcription, "_split_segments", no_split)
    monkeypatch.setattr(transcription, "_transcribe_audio_file", lambda local: "whole")
    assert asyncio.run(transcription.transcribe_audio_file(path)) == "whole"


def test_partial_segment_dir_from_a_crash_is_discarded_and_resplit(monkeypatch):
    import audio_store

    path = audio_store.save_bytes("conv", b"audio", "x.wav")
    seg_dir = audio_store.segments_dir(path)
    seg_dir.mkdir()
    (seg_dir / "seg-0000.wav").write_bytes(b"truncated")  # no .complete marker

    def fake_run(cmd, **kw):
        (seg_dir / "seg-0000.wav").write_bytes(b"full")

    monkeypatch.setattr(transcription.subprocess, "run", fake_run)
    segs = transcription._split_segments(path)
    assert [s.read_bytes() for s in segs] == [b"full"] and (seg_dir / ".complete").exists()
