"""SQLite store for canonical events. The local source of truth for everything above the adapters."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from agent_history.model import FILE_CHANGE, Event, FileChange, NormalizedSession

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
"""


def default_db_path() -> Path:
    return Path(os.environ.get("AGENT_HISTORY_DB") or DEFAULT_DB)


class Store:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def ingest(self, normalized: NormalizedSession) -> None:
        """Insert a session, replacing any earlier ingest of the same session."""
        s = normalized.session
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (s.id,))
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                            (s.id, s.agent, s.agent_version, s.cwd, s.started_at, s.ended_at,
                             s.outcome, s.source))
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
