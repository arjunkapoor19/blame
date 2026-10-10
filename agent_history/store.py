"""SQLite store for canonical events. The local source of truth for everything above the adapters."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from agent_history.model import (FILE_CHANGE, SUBAGENT_CALL, VERSION as MODEL_VERSION, Event, FileChange,
                                 NormalizedSession)

DEFAULT_DB = Path.home() / ".agent-history" / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    agent_version TEXT,
    cwd           TEXT,
    started_at    INTEGER,
    ended_at      INTEGER,
    outcome       TEXT,
    source        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    parent_thread_id TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    thread_id  TEXT,
    seq        INTEGER NOT NULL,
    input_text TEXT,
    status     TEXT,
    started_at INTEGER,
    ended_at   INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    thread_id    TEXT,
    turn_id      TEXT,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    status       TEXT,
    started_at   INTEGER,
    ended_at     INTEGER,
    parent_id    TEXT,
    payload      TEXT NOT NULL,
    source_lines TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_turn ON events(turn_id, seq);
CREATE TABLE IF NOT EXISTS file_changes (
    event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    path       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    diff       TEXT,
    move_path  TEXT,
    PRIMARY KEY (event_id, idx)
);
CREATE INDEX IF NOT EXISTS file_changes_by_path ON file_changes(path);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS synced_files (
    path     TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
);
-- Observed workspaces: an index of their files (like git's) and what changed between snapshots.
CREATE TABLE IF NOT EXISTS workspaces (
    root          TEXT PRIMARY KEY,
    first_seen    INTEGER NOT NULL,
    last_snapshot INTEGER NOT NULL  -- nanoseconds
);
CREATE TABLE IF NOT EXISTS ws_files (
    root     TEXT NOT NULL,
    path     TEXT NOT NULL,  -- relative to root
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL,
    blob     TEXT NOT NULL,
    PRIMARY KEY (root, path)
);
CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    root             TEXT NOT NULL,
    actor            TEXT NOT NULL,  -- agent | outside | baseline
    agent            TEXT,
    agent_session_id TEXT,
    tool_call_id     TEXT,
    tool_name        TEXT,
    command          TEXT,
    started_at       INTEGER,
    ended_at         INTEGER,  -- NULL while the tool call is running
    concurrent       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS observations_by_call ON observations(agent_session_id, tool_call_id);
CREATE TABLE IF NOT EXISTS observed_changes (
    observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    idx            INTEGER NOT NULL,
    path           TEXT NOT NULL,  -- absolute
    kind           TEXT NOT NULL,
    before_blob    TEXT,
    after_blob     TEXT,
    PRIMARY KEY (observation_id, idx)
);
CREATE INDEX IF NOT EXISTS observed_changes_by_path ON observed_changes(path);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("AGENT_HISTORY_DB") or DEFAULT_DB)


class Store:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.objects_dir = path.parent / "objects"  # file contents seen in observed workspaces
        self.db = sqlite3.connect(path, timeout=10)  # hooks from parallel tool calls share the database
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "source_kind" not in columns:  # databases created before sources had names
            self.db.execute("ALTER TABLE sessions ADD COLUMN source_kind TEXT")
        if self.model_version() is None and not self.db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
            self.set_model_version(MODEL_VERSION)  # a new database: nothing to re-normalize

    def close(self) -> None:
        self.db.close()

    def ingest(self, normalized: NormalizedSession, source_kind: str | None = None) -> None:
        """Insert a session, replacing any earlier ingest of it, including one from another
        source that shares its threads (the same agent run, recorded two ways)."""
        s = normalized.session
        thread_ids = [t.id for t in normalized.threads]
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (s.id,))
            self.db.executemany(
                "DELETE FROM sessions WHERE id IN (SELECT session_id FROM threads WHERE id = ?)",
                [(t,) for t in thread_ids])
            self.db.execute("INSERT INTO sessions (id, agent, agent_version, cwd, started_at, ended_at, outcome, "
                            "source, source_kind) VALUES (?,?,?,?,?,?,?,?,?)",
                            (s.id, s.agent, s.agent_version, s.cwd, s.started_at, s.ended_at,
                             s.outcome, s.source, source_kind))
            self.db.executemany("INSERT INTO threads VALUES (?,?,?)",
                                [(t.id, s.id, t.parent_thread_id) for t in normalized.threads])
            self.db.executemany("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?)",
                                [(t.id, s.id, t.thread_id, t.seq, t.input_text, t.status,
                                  t.started_at, t.ended_at) for t in normalized.turns])
            self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                [(e.id, e.session_id, e.thread_id, e.turn_id, e.seq, e.kind, e.status,
                                  e.started_at, e.ended_at, e.parent_id, json.dumps(e.payload),
                                  json.dumps(e.source_lines)) for e in normalized.events])
            self.db.executemany("INSERT INTO file_changes VALUES (?,?,?,?,?,?)",
                                [(e.id, i, c.path, c.kind, c.diff, c.move_path)
                                 for e in normalized.events for i, c in enumerate(e.file_changes)])

    # -- queries --------------------------------------------------------------

    def sessions(self) -> list[sqlite3.Row]:
        return self.db.execute("""
            SELECT s.*, COUNT(t.id) AS turn_count,
                   (SELECT input_text FROM turns WHERE session_id = s.id ORDER BY started_at, seq LIMIT 1)
                       AS first_input
            FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
            GROUP BY s.id ORDER BY s.started_at
        """).fetchall()

    def find_session(self, query: str) -> sqlite3.Row | None:
        """Exact id, or the only session whose id contains `query`."""
        rows = self.db.execute("SELECT * FROM sessions WHERE id = ? OR instr(id, ?) > 0",
                               (query, query)).fetchall()
        exact = [row for row in rows if row["id"] == query]
        if exact:
            return exact[0]
        if len(rows) > 1:
            raise LookupError(f"'{query}' matches {len(rows)} sessions; be more specific")
        return rows[0] if rows else None

    def turns(self, session_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY started_at, seq",
                               (session_id,)).fetchall()

    def turn(self, turn_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()

    def spawner(self, thread_id: str) -> Event | None:
        """The sub-agent call that started a child thread, if the agent recorded which thread it started."""
        row = self.db.execute(
            "SELECT e.* FROM events e, json_each(e.payload, '$.receiver_thread_ids') r "
            "WHERE e.kind = ? AND r.value = ? ORDER BY e.seq LIMIT 1", (SUBAGENT_CALL, thread_id)).fetchone()
        return self._event(row) if row else None

    def events(self, session_id: str | None = None, turn_id: str | None = None) -> list[Event]:
        if turn_id is not None:
            rows = self.db.execute("SELECT * FROM events WHERE turn_id = ? ORDER BY seq", (turn_id,))
        else:
            rows = self.db.execute("SELECT * FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
        return [self._event(row) for row in rows.fetchall()]

    def event(self, event_id: str) -> Event | None:
        row = self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def file_changes(self) -> list[sqlite3.Row]:
        """Every recorded file change, in the order it happened."""
        return self.db.execute("""
            SELECT f.*, e.session_id, e.turn_id, e.started_at, e.seq
            FROM file_changes f JOIN events e ON e.id = f.event_id
            WHERE e.status IS NULL OR e.status NOT IN ('declined', 'failed')
            ORDER BY e.started_at, e.session_id, e.seq, f.idx
        """).fetchall()

    def known_paths(self) -> list[sqlite3.Row]:
        """Every file agents changed or ah saw change: path, the workspace it belongs to, how many
        changes, the agents involved and when it last changed. Most recently changed first."""
        return self.db.execute("""
            SELECT path, MAX(root) AS root, COUNT(*) AS changes, GROUP_CONCAT(DISTINCT agent) AS agents,
                   MAX(at) AS last_changed
            FROM (SELECT f.path, s.cwd AS root, s.agent, e.started_at AS at
                  FROM file_changes f JOIN events e ON e.id = f.event_id JOIN sessions s ON s.id = e.session_id
                  WHERE e.status IS NULL OR e.status NOT IN ('declined', 'failed')
                  UNION ALL
                  SELECT c.path, o.root, o.agent, o.ended_at
                  FROM observed_changes c JOIN observations o ON o.id = c.observation_id
                  WHERE o.actor != 'baseline' AND o.ended_at IS NOT NULL)
            GROUP BY path ORDER BY last_changed DESC, path
        """).fetchall()

    def owner_of_threads(self, thread_ids: list[str]) -> sqlite3.Row | None:
        """The stored session that already holds any of these threads, if one does."""
        for thread_id in thread_ids:
            row = self.db.execute("SELECT s.* FROM sessions s JOIN threads t ON t.session_id = s.id "
                                  "WHERE t.id = ?", (thread_id,)).fetchone()
            if row is not None:
                return row
        return None

    def file_unchanged(self, path: str, mtime_ns: int, size: int) -> bool:
        row = self.db.execute("SELECT mtime_ns, size FROM synced_files WHERE path = ?", (path,)).fetchone()
        return row is not None and (row["mtime_ns"], row["size"]) == (mtime_ns, size)

    def mark_synced(self, path: str, mtime_ns: int, size: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO synced_files VALUES (?,?,?)", (path, mtime_ns, size))

    def model_version(self) -> int | None:
        """The model version stored sessions were normalized with (None: unknown, i.e. older)."""
        row = self.db.execute("SELECT value FROM meta WHERE key = 'model_version'").fetchone()
        return int(row["value"]) if row else None

    def set_model_version(self, version: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('model_version', ?)", (str(version),))

    def forget_synced(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM synced_files")

    # -- observed workspaces ---------------------------------------------------

    def ws_index(self, root: str) -> tuple[dict[str, tuple[int, int, str]], int | None]:
        """The stored index of `root` (path -> (mtime_ns, size, blob)) and when it was taken."""
        row = self.db.execute("SELECT last_snapshot FROM workspaces WHERE root = ?", (root,)).fetchone()
        if row is None:
            return {}, None
        entries = self.db.execute("SELECT path, mtime_ns, size, blob FROM ws_files WHERE root = ?", (root,))
        return {r["path"]: (r["mtime_ns"], r["size"], r["blob"]) for r in entries}, row["last_snapshot"]

    def ws_save(self, root: str, previous: dict, current: dict, now_ns: int) -> None:
        """Write only what changed between two indexes of `root`."""
        with self.db:
            self.db.execute("INSERT INTO workspaces VALUES (?, ?, ?) ON CONFLICT(root) DO UPDATE SET "
                            "last_snapshot = excluded.last_snapshot", (root, now_ns // 1_000_000, now_ns))
            self.db.executemany("DELETE FROM ws_files WHERE root = ? AND path = ?",
                                [(root, p) for p in previous if p not in current])
            self.db.executemany("INSERT OR REPLACE INTO ws_files VALUES (?, ?, ?, ?, ?)",
                                [(root, p, *entry) for p, entry in current.items() if previous.get(p) != entry])

    def baseline(self, path: str) -> sqlite3.Row | None:
        """What an absolute path held when observation of its workspace began, if it existed then."""
        return self.db.execute(
            "SELECT c.*, o.started_at FROM observed_changes c JOIN observations o ON o.id = c.observation_id "
            "WHERE c.path = ? AND o.actor = 'baseline' ORDER BY o.started_at LIMIT 1", (path,)).fetchone()

    def observation(self, observation_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM observations WHERE id = ?", (observation_id,)).fetchone()

    def linked_event_id(self, agent_session_id: str | None, tool_call_id: str | None) -> str | None:
        """The event for an observed tool call: same session (or thread), same native tool call id."""
        if not agent_session_id or not tool_call_id:
            return None
        row = self.db.execute(
            "SELECT e.id FROM events e WHERE json_extract(e.payload, '$.source_id') = ? "
            "AND (e.session_id = ? OR e.thread_id = ?) LIMIT 1",
            (tool_call_id, agent_session_id, agent_session_id)).fetchone()
        return row["id"] if row else None

    def observation_for(self, event: Event) -> sqlite3.Row | None:
        """The observation of an event's tool call, if its workspace was observed."""
        source_id = event.payload.get("source_id")
        if not source_id:
            return None
        return self.db.execute(
            "SELECT * FROM observations WHERE tool_call_id = ? AND agent_session_id IN (?, ?) AND actor = 'agent' "
            "ORDER BY id DESC LIMIT 1", (source_id, event.session_id, event.thread_id)).fetchone()

    def changes_of(self, observation_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM observed_changes WHERE observation_id = ? ORDER BY idx",
                               (observation_id,)).fetchall()

    def add_observation(self, root: str, actor: str, started_at: int, ended_at: int | None = None,
                        changes: list | None = None, **call) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO observations (root, actor, agent, agent_session_id, tool_call_id, tool_name, command, "
                "started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (root, actor, call.get("agent"), call.get("agent_session_id"), call.get("tool_call_id"),
                 call.get("tool_name"), call.get("command"), started_at, ended_at))
            self._add_changes(cursor.lastrowid, changes or [])
        return cursor.lastrowid

    def last_observed(self, root: str) -> int | None:
        """When anything in `root` was last observed, in milliseconds (None if never)."""
        row = self.db.execute("SELECT MAX(COALESCE(ended_at, started_at)) AS at FROM observations WHERE root = ?",
                              (root,)).fetchone()
        return row["at"]

    def add_changes(self, observation_id: int, changes: list) -> None:
        with self.db:
            self._add_changes(observation_id, changes)

    def close_observation(self, observation_id: int, ended_at: int, changes: list) -> None:
        with self.db:
            self.db.execute("UPDATE observations SET ended_at = ? WHERE id = ?", (ended_at, observation_id))
            self._add_changes(observation_id, changes)

    def open_observations(self, root: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM observations WHERE root = ? AND actor = 'agent' AND ended_at IS NULL "
                               "ORDER BY started_at", (root,)).fetchall()

    def mark_concurrent(self, observation_ids: list[int]) -> None:
        with self.db:
            self.db.executemany("UPDATE observations SET concurrent = 1 WHERE id = ?", [(i,) for i in observation_ids])

    def observed_changes(self, paths: list[str]) -> list[sqlite3.Row]:
        """Observed changes to these absolute paths (by agents or outside them), oldest first."""
        marks = ",".join("?" * len(paths))
        return self.db.execute(f"""
            SELECT c.*, o.actor, o.agent, o.agent_session_id, o.tool_call_id, o.started_at, o.ended_at
            FROM observed_changes c JOIN observations o ON o.id = c.observation_id
            WHERE o.actor != 'baseline' AND o.ended_at IS NOT NULL AND c.path IN ({marks})
            ORDER BY o.ended_at, o.id, c.idx
        """, paths).fetchall()

    def _add_changes(self, observation_id: int, changes: list) -> None:
        start = self.db.execute("SELECT COUNT(*) FROM observed_changes WHERE observation_id = ?",
                                (observation_id,)).fetchone()[0]
        self.db.executemany("INSERT INTO observed_changes VALUES (?,?,?,?,?,?)",
                            [(observation_id, start + i, c.path, c.kind, c.before, c.after)
                             for i, c in enumerate(changes)])

    def counts(self) -> dict[str, int]:
        tables = ("sessions", "threads", "turns", "events", "file_changes")
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    def _event(self, row: sqlite3.Row) -> Event:
        data: dict[str, Any] = dict(row)
        data["payload"] = json.loads(data["payload"])
        data["source_lines"] = json.loads(data["source_lines"])
        event = Event(**data)
        if event.kind == FILE_CHANGE:
            event.file_changes = [
                FileChange(r["path"], r["kind"], r["diff"], r["move_path"]) for r in self.db.execute(
                    "SELECT * FROM file_changes WHERE event_id = ? ORDER BY idx", (event.id,))]
        return event
