"""Render package-driver skills, one per enabled instance, into ~/.dop-agent/skills-gen/<skill>-<instance>/SKILL.md.

    ensure_skills(registry=None) -> list[str]   skill dir names now present (also removes stale generated ones)

Called before every Copilot session is created/resumed (copilot_session._ensure_session), so config changes show up
in new sessions without a restart. `GEN_SKILLS_DIR` is part of `bot_config.SKILL_DIRECTORIES`. Only directories this
module created (marked with `.dop-generated`) are ever deleted. A template error skips that instance (logged) and never
raises: a broken skill must not block a session.
"""
from __future__ import annotations

import fcntl
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from drivers.base import DriverError
from drivers.package import skill_suffix, template_context
from drivers.templating import render

log = logging.getLogger(__name__)

GEN_SKILLS_DIR = Path.home() / ".dop-agent" / "skills-gen"  # == bot_config.GENERATED_SKILLS_DIR
MARKER = ".dop-generated"
_LOCK = threading.Lock()


def ensure_skills(registry=None, out_dir: Path | None = None) -> list[str]:
    out = Path(out_dir) if out_dir else GEN_SKILLS_DIR
    try:
        if registry is None:
            from integrations.runtime import get_registry
            registry = get_registry()
        wanted: dict[str, str] = {}
        for it in registry.package_instances():
            man = it.manifest
            if not man.skill:
                continue
            try:
                text = render((Path(man.source_dir) / man.skill["template"]).read_text(), template_context(it))
            except (DriverError, OSError) as exc:
                log.warning("[skills] %s: not rendered: %s", it.name, exc)
                continue
            name = f"{man.skill['name']}-{skill_suffix(it.name)}"
            if name in wanted:
                log.warning("[skills] %s: skill dir %s already used by another instance; skipped", it.name, name)
                continue
            wanted[name] = text
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(out, 0o700)
        # thread lock + flock: several sessions (threads) or processes may refresh at once
        with _LOCK, open(out / ".lock", "a") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            written: list[str] = []
            for name, text in wanted.items():
                try:
                    _write_skill(out / name, text)
                    written.append(name)
                except OSError:
                    log.exception("[skills] %s: write failed", name)
            for d in out.iterdir():
                if d.is_dir() and not d.is_symlink() and d.name not in wanted and (d / MARKER).exists():
                    shutil.rmtree(d, ignore_errors=True)
        return sorted(written)
    except Exception:
        log.exception("[skills] rendering failed")
        return []


def _write_skill(d: Path, text: str) -> None:
    d.mkdir(exist_ok=True)
    (d / MARKER).touch()
    f = d / "SKILL.md"
    if f.exists() and f.read_text() == text:
        return
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".SKILL.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, f)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
