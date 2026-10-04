"""Where agent history comes from, and keeping the store in sync with it.

Each `Source` knows how to find one kind of agent record on this machine and turn
it into canonical events. `SOURCES` is the only place adapters are registered:
supporting a new agent means writing its adapter and adding one entry here.

When two sources hold the same agent run (they share thread ids), the one with
the higher priority wins, so a richer recording is never replaced by a poorer one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from agent_history.adapters import codex, codex_rollout
from agent_history.model import NormalizedSession
from agent_history.store import Store

INGESTED, SKIPPED, FAILED = "ingested", "skipped", "failed"


@dataclass(frozen=True)
class Source:
    name: str
    priority: int  # higher wins when two sources hold the same session
    discover: Callable[[], Iterable[Path]]  # records to sync automatically; may be empty
    matches: Callable[[Path], bool]  # is this path one of ours? (for explicit `ah ingest PATH`)
    normalize: Callable[[Path], NormalizedSession]


@dataclass
class Result:
    path: Path
    source: str
    status: str  # INGESTED | SKIPPED | FAILED
    session_id: str | None = None
    reason: str | None = None


def _codex_logs() -> Iterable[Path]:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return sorted((home / "sessions").glob("**/rollout-*.jsonl"))


SOURCES: list[Source] = [
    Source("codex-capture", 2, discover=lambda: [],
           matches=lambda p: p.is_dir() and (p / "events.jsonl").is_file(), normalize=codex.normalize),
    Source("codex-log", 1, discover=_codex_logs,
           matches=lambda p: p.is_file() and p.name.startswith("rollout-") and p.suffix == ".jsonl",
           normalize=codex_rollout.normalize),
]


def ingest(store: Store, path: Path, sources: list[Source] = SOURCES) -> Result:
    """Ingest one explicitly named path with the first source that recognises it."""
    source = next((s for s in sources if s.matches(path)), None)
    if source is None:
        raise LookupError(f"{path}: not a recognised agent record")
    return _ingest(store, source, path, sources)


def sync(store: Store, sources: list[Source] = SOURCES) -> list[Result]:
    """Ingest every discoverable record that is new or changed since the last sync."""
    results = []
    for source in sources:
        for path in source.discover():
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path.resolve())
            if store.file_unchanged(key, stat.st_mtime_ns, stat.st_size):
                continue
            results.append(_ingest(store, source, path, sources))
            store.mark_synced(key, stat.st_mtime_ns, stat.st_size)  # failures too, until the file changes
    return results


def _ingest(store: Store, source: Source, path: Path, sources: list[Source]) -> Result:
    try:
        normalized = source.normalize(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return Result(path, source.name, FAILED, reason=f"{type(error).__name__}: {error}")
    owner = store.owner_of_threads([t.id for t in normalized.threads])
    if owner is not None:
        holder = _source_of(owner, sources)
        if holder is not None and holder.priority > source.priority:
            return Result(path, source.name, SKIPPED, owner["id"], f"already recorded by {holder.name}")
    store.ingest(normalized, source.name)
    return Result(path, source.name, INGESTED, normalized.session.id)


def _source_of(session_row, sources: list[Source]) -> Source | None:
    """The source a stored session came from (inferred from its path for rows stored before
    sessions recorded their source)."""
    by_name = {s.name: s for s in sources}
    if session_row["source_kind"] in by_name:
        return by_name[session_row["source_kind"]]
    path = Path(session_row["source"])
    return next((s for s in sources if path.exists() and s.matches(path)), None)
