"""Snapshots of a workspace's files, so `ah` can see exactly what changed, whatever changed it.

Agents only report changes made through their edit tools; a shell command's side effects
(`printf >> f`, `sed -i`, a script that writes files) are invisible in their logs. So around
each tool call `ah` snapshots the workspace itself and records the difference.

A snapshot works like git's index: every file is `stat`ed, and only files whose size or
modification time changed (or that changed too recently to trust the timestamp) are read
and hashed. Contents are kept in a content-addressed blob store next to the database.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from agent_history import model

MAX_BLOB = 1_000_000  # larger files are tracked by hash only, without content
MAX_FILES = 50_000
IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
                ".mypy_cache", ".pytest_cache", ".tox", ".next"}
RACY_NS = 2_000_000_000  # files modified this close to the last snapshot are re-read anyway


@dataclass
class Change:
    path: str  # absolute
    kind: str  # model.ADD | model.UPDATE | model.DELETE
    before: str | None  # blob ids
    after: str | None


class Blobs:
    """Content-addressed file contents: <dir>/ab/cdef… (sha256 of the content, zlib-compressed)."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def put(self, data: bytes) -> str:
        blob = hashlib.sha256(data).hexdigest()
        if len(data) <= MAX_BLOB and b"\0" not in data[:8000]:  # keep text, not binaries
            path = self.dir / blob[:2] / blob[2:]
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(zlib.compress(data))
                tmp.replace(path)
        return blob

    def get(self, blob: str | None) -> bytes | None:
        """The content, or None if it wasn't kept (binary, too large) or `blob` is None."""
        if not blob:
            return None
        try:
            return zlib.decompress((self.dir / blob[:2] / blob[2:]).read_bytes())
        except OSError:
            return None

    def text(self, blob: str | None) -> str | None:
        data = self.get(blob)
        return data.decode("utf-8", errors="replace") if data is not None else None


def root_of(cwd: str | Path) -> Path:
    """The workspace a path belongs to: its git repository's top level, or the directory itself."""
    cwd = Path(cwd).resolve()
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return cwd


def list_files(root: Path) -> list[str]:
    """Files in the workspace, relative to `root`: git's view (tracked + untracked, minus ignored)
    in a repository, otherwise a walk that skips common build and dependency directories."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                             capture_output=True, timeout=30)
        if out.returncode == 0:
            return [p for p in out.stdout.decode("utf-8", errors="surrogateescape").split("\0") if p][:MAX_FILES]
    except (OSError, subprocess.TimeoutExpired):
        pass
    files: list[str] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in names:
            files.append(os.path.relpath(os.path.join(directory, name), root))
            if len(files) >= MAX_FILES:
                return files
    return files


def snapshot(store, root: Path) -> tuple[bool, list[Change]]:
    """Bring the stored index of `root` up to date and return (baseline, changes). The first
    snapshot of a workspace is a baseline: every file is reported as added, which records
    what the workspace held before anything was observed."""
    blobs = Blobs(store.objects_dir)
    previous, last_ns = store.ws_index(str(root))
    baseline = last_ns is None
    now_ns = time.time_ns()
    current: dict[str, tuple[int, int, str]] = {}
    changes: list[Change] = []
    for rel in list_files(root):
        full = root / rel
        try:
            info = full.lstat()
        except OSError:
            continue
        if not full.is_file() or full.is_symlink():
            continue
        before = previous.get(rel)
        trusted = before is not None and (before[0], before[1]) == (info.st_mtime_ns, info.st_size) \
            and last_ns is not None and info.st_mtime_ns < last_ns - RACY_NS
        if trusted:
            current[rel] = before
            continue
        try:
            blob = blobs.put(full.read_bytes())
        except OSError:
            continue
        current[rel] = (info.st_mtime_ns, info.st_size, blob)
        if before is None:
            changes.append(Change(str(full), model.ADD, None, blob))
        elif before[2] != blob:
            changes.append(Change(str(full), model.UPDATE, before[2], blob))
    changes += [Change(str(root / rel), model.DELETE, entry[2], None)
                for rel, entry in previous.items() if rel not in current]
    store.ws_save(str(root), previous, current, now_ns)
    return baseline, changes


def unified_diff(before: str, after: str) -> str:
    """Hunks (no file headers) turning `before` into `after`, with line numbers."""
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++"))) + "\n"
