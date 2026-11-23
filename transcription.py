"""
Voice / audio message transcription: Slack audio-file detection, download,
and local Whisper (openai-whisper) transcription.

Depends on bot_config (SLACK_BOT_TOKEN, WHISPER_MODEL, log) and audio_store;
never on dop_agent.py.
"""

from __future__ import annotations

import asyncio
import os

import config
import shutil
import subprocess
from pathlib import Path

import audio_store

from bot_config import SLACK_BOT_TOKEN, WHISPER_MODEL, log

AUDIO_MIME_PREFIX = "audio/"
# Slack sometimes tags voice notes without a proper audio/* mimetype but
# still marks them with subtype "slack_audio" or filetype "m4a"/"mp4".
AUDIO_SLACK_SUBTYPES = {"slack_audio"}
AUDIO_FILETYPES = {"m4a", "mp3", "wav", "ogg", "webm", "mp4", "aac", "flac"}

_whisper_model_loaded = False

# Hard cap on a single audio upload (config agent.transcription.audio_max_bytes). Audio
# is streamed to disk and long files are split into SEGMENT_SECONDS chunks that
# each take the shared Whisper lock separately, so a long recording cannot
# starve other transcriptions (Slack and Web UI share one lock).
MAX_AUDIO_BYTES = config.value("agent.transcription.audio_max_bytes")
SEGMENT_SECONDS = 600


def _is_audio_file(f: dict) -> bool:
    mimetype = (f.get("mimetype") or "").lower()
    if mimetype.startswith(AUDIO_MIME_PREFIX):
        return True
    if f.get("subtype") in AUDIO_SLACK_SUBTYPES:
        return True
    return (f.get("filetype") or "").lower() in AUDIO_FILETYPES


def _is_slack_files_url(url: str) -> bool:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "files.slack.com"


def _download_slack_file(url: str, token: str) -> bytes:
    """Bot token is sent only to https://files.slack.com; redirects are followed
    manually and the token is dropped for any other host."""
    import requests
    from urllib.parse import urljoin
    headers = {"Authorization": f"Bearer {token}"} if _is_slack_files_url(url) else {}
    resp = None
    for _ in range(6):
        resp = requests.get(url, headers=headers, timeout=60, allow_redirects=False)
        if not getattr(resp, "is_redirect", False):
            break
        url = urljoin(url, resp.headers.get("Location", ""))
        if not _is_slack_files_url(url):
            headers = {}
    else:
        raise RuntimeError("too many redirects downloading Slack file")
    resp.raise_for_status()
    return resp.content


_whisper_model = None

# openai-whisper's model.transcribe() is not safe to call concurrently from
# multiple threads on the same model instance; concurrent calls make the second
# transcription silently fail. This lock serializes all transcriptions into a
# FIFO queue.
_whisper_transcribe_lock = asyncio.Lock()


def _get_whisper_model():
    """Load (and cache) the openai-whisper model. Weights are fetched from
    OpenAI's official Azure CDN (openaipublic.azureedge.net) on first use,
    which — unlike huggingface.co — is not blocked by the corporate firewall.
    """
    global _whisper_model
    if _whisper_model is None:
        import whisper
        _whisper_model = whisper.load_model(WHISPER_MODEL)
    return _whisper_model


def whisper_status() -> dict:
    """Report whether voice transcription can run in this process, with a fix hint."""
    import importlib.util
    import shutil
    import sys
    problems = []
    if importlib.util.find_spec("whisper") is None:
        problems.append({"message": "Python package 'openai-whisper' is not installed.",
                         "command": f"{sys.executable} -m pip install -U openai-whisper"})
    if shutil.which("ffmpeg") is None:
        problems.append({"message": "ffmpeg is not on PATH.", "command": "brew install ffmpeg"})
    return {"ok": not problems, "problems": problems}


def _transcribe_audio_file(local_path: str) -> str:
    """Blocking call — run in a thread executor."""
    global _whisper_model_loaded
    model = _get_whisper_model()
    result = model.transcribe(local_path)
    _whisper_model_loaded = True
    return (result.get("text") or "").strip()


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _split_segments(path: Path) -> list[Path]:
    """Cut ``path`` into SEGMENT_SECONDS 16 kHz mono wav pieces (blocking)."""
    seg_dir = audio_store.segments_dir(path)
    done_marker = seg_dir / ".complete"
    if done_marker.exists():
        return sorted(seg_dir.glob("seg-*.wav"))
    shutil.rmtree(seg_dir, ignore_errors=True)  # partial split from a crash
    seg_dir.mkdir(mode=0o700)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-ar", "16000", "-ac", "1",
         "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), str(seg_dir / "seg-%04d.wav")],
        check=True, capture_output=True, timeout=3600,
    )
    done_marker.touch()
    return sorted(seg_dir.glob("seg-*.wav"))


async def transcribe_audio_file(path, *, on_queued=None, on_progress=None) -> str:
    """Transcribe an audio file that is already on disk. NEVER deletes it.

    Audio longer than SEGMENT_SECONDS is split with ffmpeg; each segment takes
    the shared Whisper lock on its own (so other jobs interleave) and its text is
    cached in the sidecar ``texts`` so a resumed job skips finished segments.
    ``on_queued()`` is awaited if the lock is busy; ``on_progress(done, total)``
    after each segment.
    """
    path = Path(path)
    loop = asyncio.get_running_loop()

    async def _locked(local_path) -> str:
        nonlocal queued_notified
        if on_queued is not None and not queued_notified and _whisper_transcribe_lock.locked():
            queued_notified = True
            try:
                await on_queued()
            except Exception:
                log.debug("on_queued callback failed", exc_info=True)
        async with _whisper_transcribe_lock:
            return await loop.run_in_executor(None, _transcribe_audio_file, str(local_path))

    queued_notified = False
    if on_queued is not None and _whisper_transcribe_lock.locked():
        queued_notified = True
        try:
            await on_queued()
        except Exception:
            log.debug("on_queued callback failed", exc_info=True)
    duration = await loop.run_in_executor(None, _probe_duration, path)
    if duration is not None and duration <= SEGMENT_SECONDS:
        return await _locked(path)

    # Unknown duration is the norm for browser MediaRecorder webm (no duration in
    # the header), so we must split those too — an hour-long recording must not
    # go to Whisper in one piece.
    try:
        segments = await loop.run_in_executor(None, _split_segments, path)
    except Exception:
        if duration is not None:
            raise
        log.warning("ffmpeg could not segment %s; transcribing it whole", path, exc_info=True)
        segments = []
    if not segments:
        return await _locked(path)
    texts = list(audio_store.read(path).get("texts") or [])
    for i, seg in enumerate(segments):
        if i < len(texts):
            continue
        texts.append(await _locked(seg))
        audio_store.update(path, texts=texts, segments_total=len(segments))
        if on_progress is not None:
            try:
                await on_progress(i + 1, len(segments))
            except Exception:
                log.debug("on_progress callback failed", exc_info=True)
    return " ".join(t for t in texts if t).strip()


async def _transcribe_adhoc(path: Path, on_queued) -> str:
    """Transcribe a job nobody else tracks (no placeholder to resume): keep the
    file and record the outcome in its sidecar so it is never silently lost."""
    try:
        text = await transcribe_audio_file(path, on_queued=on_queued)
    except Exception as exc:
        audio_store.update(path, state="failed", error=str(exc))
        raise
    audio_store.mark_done(path, transcript=text)
    return text


async def transcribe_audio_bytes(raw: bytes, filename: str, *, on_queued=None,
                                 conversation_id: str | None = None) -> str:
    """For callers that only hold bytes: persist them to the audio store
    first, transcribe from that file, and mark the job done.
    Raises ``ValueError`` over ``MAX_AUDIO_BYTES``."""
    if len(raw) > MAX_AUDIO_BYTES:
        raise ValueError(f"Audio file too large ({len(raw)} bytes, max {MAX_AUDIO_BYTES})")
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(
        None, lambda: audio_store.save_bytes(conversation_id or "_adhoc", raw, filename, default_ext="m4a", platform="adhoc"))
    return await _transcribe_adhoc(path, on_queued)


async def save_slack_audio(file_info: dict, conversation_id: str | None = None, *,
                           platform: str = "slack", **meta) -> Path:
    """Download a Slack audio file and persist it to the audio store (state=pending)."""
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise RuntimeError("Slack file has no url_private to download from")
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(None, _download_slack_file, url, SLACK_BOT_TOKEN)
    if len(raw) > MAX_AUDIO_BYTES:
        raise ValueError(f"Audio file too large ({len(raw)} bytes, max {MAX_AUDIO_BYTES})")
    return await loop.run_in_executor(
        None, lambda: audio_store.save_bytes(
            conversation_id or "_adhoc", raw, file_info.get("name") or "audio",
            default_ext="m4a", platform=platform, file_info=file_info, **meta))


async def transcribe_slack_audio(file_info: dict, *, on_queued=None, conversation_id: str | None = None) -> str:
    """Download a Slack audio file, persist it, and transcribe it from disk.

    ``on_queued`` is awaited if the shared Whisper lock is busy. Used by the
    restart fallback for pending entries that have no local file.
    """
    path = await save_slack_audio(file_info, conversation_id, platform="adhoc")
    return await _transcribe_adhoc(path, on_queued)
