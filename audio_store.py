"""
Durable on-disk store for voice recordings + a small per-file sidecar job record.

Why this exists: a bot restart mid-transcription, an oversized upload or a
crash must not lose a recording. The raw audio is written to
``~/.dop-agent/audio/<conversation_id>/<YYYYmmdd-HHMMSS>-<4hex>.<ext>`` FIRST,
and only then is transcription started, from that path.

Each audio file has a sidecar ``<file>.json`` (single source of truth for the job):

  state      uploading -> pending -> transcribing -> transcribed -> done | failed
  attempts   how many times transcription was started (resume gives up at MAX_ATTEMPTS)
  transcript / texts   final text / per-segment texts (resume skips finished segments)
  delivered  True once the transcript was delivered to the conversation
  message_id / message_ref / thread_key / platform / user_id / file_info

Rules: audio is never deleted before the transcript is delivered; ``cleanup()``
only removes ``done`` + ``delivered`` jobs older than the retention window;
anything else (pending/transcribing/failed) is kept for resume/manual recovery.
Names are server-generated and extensions come from an allow-list, so nothing
client-controlled ends up in a path. Chunked browser uploads (``uploading``)
append to the same file; ``stale_uploading()`` finds ones whose tab went away.
"""

from __future__ import annotations

import json
import os

import config
import re
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / ".dop-agent" / "audio"
ALLOWED_EXTS = {"webm", "ogg", "m4a", "mp3", "wav", "mp4", "aac", "flac"}
RESUMABLE_STATES = {"pending", "transcribing", "transcribed"}
MAX_ATTEMPTS = 3
RETENTION_DAYS = config.value("agent.transcription.audio_retention_days")
UPLOAD_IDLE_SECONDS = 90
_CONV_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UPLOAD_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_lock = threading.RLock()
_active_copies: set[str] = set()  # streamed uploads still being written by this process


class AudioTooLarge(Exception):
    pass


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def segments_dir(path: Path) -> Path:
    return path.with_name(path.name + ".segments")


def safe_ext(filename: str | None, default: str = "webm") -> str:
    ext = Path(filename or "").suffix.lower().lstrip(".")
    return ext if ext in ALLOWED_EXTS else default


def _conv_dir(conversation_id: str) -> Path:
    if not _CONV_RE.match(conversation_id or ""):
        raise ValueError(f"Invalid conversation id for audio path: {conversation_id!r}")
    d = ROOT / conversation_id
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)
    os.chmod(d, 0o700)
    return d


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read(path: Path) -> dict:
    try:
        return json.loads(sidecar_path(path).read_text())
    except (OSError, ValueError):
        return {}


def update(path: Path, **fields) -> dict:
    with _lock:
        data = read(path)
        data.update(fields)
        data.setdefault("updated_at", 0)
        if "updated_at" not in fields:
            data["updated_at"] = time.time()
        _write_json(sidecar_path(path), data)
        return data


def _open_nofollow(path: Path, flags: int, mode: str):
    """Open an existing upload file without following a symlink planted at its path."""
    fd = os.open(path, flags | os.O_NOFOLLOW)
    return os.fdopen(fd, mode)


def _create(conversation_id: str, ext: str, meta: dict, state: str) -> Path:
    d = _conv_dir(conversation_id)
    while True:
        stem = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        path = d / f"{stem}.{ext}"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        break
    update(path, state=state, attempts=0, delivered=False, conversation_id=conversation_id,
           created_at=time.time(), next_seq=0, bytes=0, **meta)
    return path


def save_bytes(conversation_id: str, raw: bytes, filename: str | None = None, *,
               default_ext: str = "webm", **meta) -> Path:
    """Persist ``raw`` and return its path; state=pending once fully written."""
    path = _create(conversation_id, safe_ext(filename, default_ext), {"name": filename, **meta}, "uploading")
    with _open_nofollow(path, os.O_WRONLY | os.O_TRUNC, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    update(path, state="pending", bytes=len(raw))
    return path


def copy_fileobj(conversation_id: str, fileobj, filename: str | None, max_bytes: int, *,
                 default_ext: str = "webm", **meta) -> Path:
    """Stream ``fileobj`` to disk in 1 MB chunks (blocking — call in a thread).
    Raises AudioTooLarge (and removes the partial file) over ``max_bytes``."""
    path = _create(conversation_id, safe_ext(filename, default_ext), {"name": filename, **meta}, "uploading")
    total = 0
    _active_copies.add(str(path))
    try:
        with _open_nofollow(path, os.O_WRONLY | os.O_TRUNC, "wb") as out:
            n = 0
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AudioTooLarge(f"Audio file too large (> {max_bytes} bytes)")
                out.write(chunk)
                n += 1
                if n % 8 == 0:
                    update(path, bytes=total)  # heartbeat: keeps the upload "fresh"
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        if total > max_bytes or total == 0:
            remove(path)
        raise
    finally:
        _active_copies.discard(str(path))
    if total == 0:
        remove(path)
        raise ValueError("Empty audio upload")
    update(path, state="pending", bytes=total)
    return path


def begin_chunked(conversation_id: str, filename: str | None, **meta) -> Path:
    return _create(conversation_id, safe_ext(filename), {"name": filename, **meta}, "uploading")


def resolve_upload(conversation_id: str, upload_id: str) -> Path | None:
    if not _CONV_RE.match(conversation_id or "") or not _UPLOAD_ID_RE.match(upload_id or ""):
        return None
    for ext in ALLOWED_EXTS:
        p = ROOT / conversation_id / f"{upload_id}.{ext}"
        if p.is_symlink():
            continue
        if p.exists() and p.resolve().is_relative_to(ROOT.resolve()):
            return p
    return None


def append_chunk(path: Path, seq: int, data: bytes, max_bytes: int) -> int:
    """Append one chunk of a chunked upload (idempotent per ``seq``). Returns
    next expected seq. Raises ValueError (not uploading / gap) or AudioTooLarge."""
    with _lock:
        meta = read(path)
        if meta.get("state") != "uploading":
            raise ValueError(f"upload is {meta.get('state')!r}, not accepting chunks")
        nxt = meta.get("next_seq", 0)
        if seq < nxt:
            return nxt
        if seq > nxt:
            raise ValueError(f"chunk gap: expected seq {nxt}, got {seq}")
        if meta.get("bytes", 0) + len(data) > max_bytes:
            raise AudioTooLarge(f"Audio file too large (> {max_bytes} bytes)")
        with _open_nofollow(path, os.O_WRONLY | os.O_APPEND, "ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        update(path, next_seq=nxt + 1, bytes=meta.get("bytes", 0) + len(data))
        return nxt + 1


def finish_chunked(path: Path) -> bool:
    """uploading -> pending. False if nothing was received (file removed)."""
    with _lock:
        meta = read(path)
        if meta.get("state") != "uploading":
            return meta.get("state") in RESUMABLE_STATES | {"done"}
        if not meta.get("bytes"):
            remove(path)
            return False
        update(path, state="pending")
        return True


def abort_chunked(path: Path) -> bool:
    with _lock:
        if read(path).get("state") != "uploading":
            return False
        remove(path)
        return True


def remove(path: Path) -> None:
    for p in (path, sidecar_path(path)):
        try:
            p.unlink()
        except OSError:
            pass
    shutil.rmtree(segments_dir(path), ignore_errors=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def mark_done(path: Path, **fields) -> None:
    update(path, state="done", delivered=True, **fields)
    shutil.rmtree(segments_dir(path), ignore_errors=True)


def _all_jobs():
    if not ROOT.exists():
        return
    for sc in sorted(ROOT.glob("*/*.json")):
        audio = sc.with_name(sc.name[:-5])
        if audio.exists():
            yield audio, read(audio)


def list_unfinished() -> list[tuple[Path, dict]]:
    """Jobs that must be (re)started: pending/transcribing/transcribed."""
    return [(p, m) for p, m in _all_jobs() if m.get("state") in RESUMABLE_STATES]


def stale_uploading(idle_seconds: float = UPLOAD_IDLE_SECONDS) -> list[tuple[Path, dict]]:
    """Uploads nobody has written to lately (tab closed, bot restarted mid-copy)
    that are not being written by this process right now."""
    now = time.time()
    return [(p, m) for p, m in _all_jobs()
            if m.get("state") == "uploading" and str(p) not in _active_copies
            and now - m.get("updated_at", 0) > idle_seconds]


def promote_stale() -> list[tuple[Path, dict]]:
    """Abandoned uploads become normal pending jobs using whatever reached disk
    (judged by the real file size, not the sidecar counter); empty ones are removed."""
    out = []
    for p, _m in stale_uploading():
        with _lock:
            if read(p).get("state") != "uploading":
                continue
            size = p.stat().st_size
            if size == 0:
                remove(p)
            else:
                out.append((p, update(p, state="pending", bytes=size)))
    return out


def retry_failed(*, startup: bool = False, conversation_id: str | None = None) -> list[Path]:
    """Re-queue ``failed`` jobs (audio is always kept). At startup each job is
    retried at most once (``startup_retries``); explicit retries reset attempts."""
    out = []
    for p, m in _all_jobs():
        if m.get("state") != "failed":
            continue
        if conversation_id and m.get("conversation_id") != conversation_id:
            continue
        if startup and m.get("startup_retries", 0) >= 1:
            continue
        update(p, state="pending", attempts=0, error=None, startup_retries=m.get("startup_retries", 0) + (1 if startup else 0))
        out.append(p)
    return out


def cleanup(retention_days: float | None = None) -> int:
    """Delete done+delivered jobs older than the retention window."""
    days = RETENTION_DAYS if retention_days is None else retention_days
    cutoff = time.time() - days * 86400
    removed = 0
    for p, m in list(_all_jobs()):
        if m.get("state") == "done" and m.get("delivered") and m.get("updated_at", 0) < cutoff:
            remove(p)
            removed += 1
    return removed
