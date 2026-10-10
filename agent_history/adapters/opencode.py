"""opencode sessions (`~/.local/share/opencode/opencode.db`) -> canonical events.

opencode keeps every session in one SQLite database. The tables used here:

  session  one row per session: id, directory, opencode version, times
  message  one row per message, JSON `data`: `role` user (a prompt; starts a turn) or
           assistant (one model reply; `parentID` is the prompt it answers, `error` says
           how it failed or was aborted)
  part     one row per piece of a message, JSON `data`: `text`, `reasoning`, `tool`
           (`callID`, `tool`, `state` with input, output, metadata, status and times),
           `step-finish` (token usage of one model call), and bookkeeping (`step-start`,
           `patch` snapshots, ...) that isn't agent behaviour

One database holds many sessions, so a session is addressed by the virtual path
`<database>/<session id>`. A sub-agent (the `task` tool) runs as a child session, with
`parent_id` set: it becomes a child thread of its parent's session, so only top-level
sessions are sessions in ah. Records have no line numbers: events carry their part id.
What is kept and why is in docs/event-model.md.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_history import model
from agent_history.model import Event, FileChange, NormalizedSession, Session, Thread, Turn

AGENT = "opencode"

FILE_TOOLS = {"edit", "write", "patch", "multiedit", "apply_patch"}
READ_TOOLS = {"read": model.READ, "grep": model.SEARCH, "glob": model.LIST, "list": model.LIST}
SUBAGENT_TOOLS = {"task"}

ABORTED = "MessageAbortedError"
DECLINED = ("rejected permission", "specified a rule which prevents you")


def normalize(path: str | Path) -> NormalizedSession:
    path = Path(path)
    with connect(path.parent) as db:
        return _Normalizer(db, path).run()


def connect(database: Path) -> sqlite3.Connection:
    """Read-only, so ah never takes a write lock on opencode's database."""
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def sessions(database: Path) -> list[tuple[str, int]]:
    """(session id, last update in ms) for every top-level session in the database. A session
    counts as updated when any of its sub-agent sessions is."""
    try:
        with connect(database) as db:
            rows = db.execute("SELECT id, parent_id, time_updated FROM session").fetchall()
    except sqlite3.Error:
        return []
    parents = {row["id"]: row["parent_id"] for row in rows}
    updated: dict[str, int] = {}
    for row in rows:
        root = _root(row["id"], parents)
        updated[root] = max(updated.get(root, 0), row["time_updated"])
    return sorted(updated.items())


def _root(session_id: str, parents: dict[str, str | None]) -> str:
    """The top-level session above `session_id` (a parent missing from the database ends the walk)."""
    seen = {session_id}
    while parents.get(session_id) in parents and parents[session_id] not in seen:
        session_id = parents[session_id]
        seen.add(session_id)
    return session_id


class _Normalizer:
    def __init__(self, db: sqlite3.Connection, path: Path) -> None:
        self.db = db
        self.path = path
        self.session: Session | None = None
        self.threads: list[Thread] = []
        self.thread = ""  # the opencode session (thread) whose records are being read
        self.turns: dict[str, Turn] = {}  # user message id -> turn
        self.events: list[Event] = []
        self.totals: dict[str, dict[str, int]] = {}  # thread -> cumulative token counts
        self.outcome: dict[str, str] = {}  # turn id -> how its last reply ended

    def run(self) -> NormalizedSession:
        parents = {row["id"]: row["parent_id"] for row in self.db.execute("SELECT id, parent_id FROM session")}
        if self.path.name not in parents:
            raise ValueError(f"{self.path}: no such opencode session")
        root = self.db.execute("SELECT * FROM session WHERE id = ?", (_root(self.path.name, parents),)).fetchone()
        self.session = Session(id=root["id"], agent=AGENT, agent_version=root["version"], cwd=root["directory"],
                               started_at=root["time_created"], ended_at=root["time_updated"], outcome=None,
                               source=str(self.path.parent / root["id"]))
        tree = [(root, None)]
        for row, parent in tree:  # grows as sub-agent sessions are found
            self.threads.append(Thread(row["id"], root["id"], parent))
            self.session.ended_at = max(self.session.ended_at or 0, row["time_updated"])
            self._read(row["id"])
            tree += [(child, row["id"]) for child in self.db.execute(
                "SELECT * FROM session WHERE parent_id = ? ORDER BY time_created, id", (row["id"],))]
        return self._finish()

    def _read(self, session_id: str) -> None:
        """One opencode session's messages, as the thread `session_id`."""
        self.thread = session_id
        parts: dict[str, list[dict[str, Any]]] = {}
        for part in self.db.execute("SELECT id, message_id, data FROM part WHERE session_id = ? ORDER BY id",
                                    (session_id,)):
            parts.setdefault(part["message_id"], []).append({**_json(part["data"]), "id": part["id"]})
        for message in self.db.execute("SELECT id, time_created, data FROM message WHERE session_id = ? "
                                       "ORDER BY time_created, id", (session_id,)):
            data = _json(message["data"])
            if data.get("role") == "user":
                self._prompt(message["id"], message["time_created"], data, parts.get(message["id"], []))
            elif data.get("role") == "assistant":
                self._reply(message["id"], data, parts.get(message["id"], []))

    # -- messages ---------------------------------------------------------------

    def _prompt(self, message_id: str, created: int, data: dict[str, Any], parts: list[dict[str, Any]]) -> None:
        text = "\n".join(p.get("text", "") for p in parts
                         if p.get("type") == "text" and not p.get("synthetic") and not p.get("ignored"))
        seq = sum(1 for t in self.turns.values() if t.thread_id == self.thread) + 1
        turn = Turn(message_id, self.thread, seq, input_text=text, started_at=created)
        self.turns[message_id] = turn
        self._add(turn, model.USER_MESSAGE, "completed", created, created, {"text": text}, message_id)

    def _reply(self, message_id: str, data: dict[str, Any], parts: list[dict[str, Any]]) -> None:
        mine = [t for t in self.turns.values() if t.thread_id == self.thread]
        turn = self.turns.get(data.get("parentID")) or (mine[-1] if mine else None)
        created = (data.get("time") or {}).get("created")
        completed = (data.get("time") or {}).get("completed")
        error = data.get("error") or {}
        if turn is not None:
            if error.get("name") == ABORTED:
                self.outcome[turn.id] = "interrupted"
            elif error:
                self.outcome[turn.id] = "failed"
            elif data.get("finish") and data["finish"] != "tool-calls":
                self.outcome[turn.id] = "completed"
            else:
                self.outcome[turn.id] = "incomplete"
        for part in parts:
            kind = part.get("type")
            times = part.get("time") or {}
            start, end = times.get("start") or created, times.get("end") or completed
            if kind == "text" and part.get("text", "").strip() and not part.get("synthetic"):
                self._add(turn, model.AGENT_MESSAGE, "completed", start, end,
                          {"text": part["text"], "phase": "commentary", "model": data.get("modelID")}, part["id"])
            elif kind == "reasoning" and part.get("text", "").strip():
                self._add(turn, model.REASONING, "completed", start, end,
                          {"summary": [], "content": [part["text"]]}, part["id"])
            elif kind == "tool":
                self._tool(turn, part, created)
            elif kind == "step-finish" and part.get("tokens"):
                self._add(turn, model.TOKEN_USAGE, None, completed, completed,
                          self._usage(part["tokens"], data.get("modelID")), part["id"])
        if error and error.get("name") != ABORTED:
            message = (error.get("data") or {}).get("message") or error.get("name")
            self._add(turn, model.ERROR, "failed", completed, completed,
                      {"message": message, "name": error.get("name")}, message_id)

    def _tool(self, turn: Turn | None, part: dict[str, Any], created: int | None) -> None:
        state = part.get("state") or {}
        times = state.get("time") or {}
        kind, payload = _tool_call(part)
        status = _status(state)
        event = self._add(turn, kind, status, times.get("start") or created, times.get("end"), payload, part["id"])
        metadata = state.get("metadata") or {}
        output = state.get("output") if status == "completed" else state.get("error")
        if kind == model.COMMAND:
            payload["exit_code"] = metadata.get("exit") if isinstance(metadata.get("exit"), int) else None
            if status == "completed" and payload["exit_code"] not in (None, 0):
                event.status = "failed"  # opencode marks a command completed whatever its exit code
            payload["output"] = metadata.get("output") or output
        elif kind == model.FILE_CHANGE:
            if status == "completed":
                event.file_changes = _file_changes(part.get("tool"), state)
            else:
                payload["result"] = output
        elif kind == model.SUBAGENT_CALL:
            payload["result"] = output
            payload["receiver_thread_ids"] = [metadata["sessionId"]] if metadata.get("sessionId") else []
        elif part.get("tool") not in READ_TOOLS:
            payload["result"] = output  # file contents from read tools aren't kept

    # -- helpers ----------------------------------------------------------------

    def _add(self, turn: Turn | None, kind: str, status: str | None, start: int | None, end: int | None,
             payload: dict[str, Any], record_id: str | None) -> Event:
        if record_id:
            payload["record_id"] = record_id  # the opencode part or message this event came from
        event = Event(id=f"{self.session.id}:{len(self.events) + 1}", session_id=self.session.id,
                      thread_id=self.thread, turn_id=turn.id if turn else None, seq=len(self.events) + 1,
                      kind=kind, status=status, started_at=start, ended_at=end if end is not None else start,
                      payload=payload)
        self.events.append(event)
        return event

    def _usage(self, tokens: dict[str, Any], model_id: str | None) -> dict[str, Any]:
        cache = tokens.get("cache") or {}
        cached = cache.get("read") or 0
        last = {
            "input": (tokens.get("input") or 0) + cached + (cache.get("write") or 0),
            "cached_input": cached,
            "output": tokens.get("output") or 0,
            "reasoning_output": tokens.get("reasoning"),
        }
        last["total"] = last["input"] + last["output"]
        totals = self.totals.setdefault(self.thread, {})
        for key in ("input", "cached_input", "output", "total"):
            totals[key] = totals.get(key, 0) + last[key]
        return {"total": dict(totals), "last": last, "model": model_id}

    def _finish(self) -> NormalizedSession:
        turns = sorted(self.turns.values(), key=lambda t: (t.started_at or 0, t.seq))
        for turn in turns:
            turn.status = self.outcome.get(turn.id, "incomplete")
            in_turn = [e for e in self.events if e.turn_id == turn.id]
            turn.ended_at = max((e.ended_at or e.started_at or 0 for e in in_turn), default=turn.started_at)
            for event in in_turn:
                if event.status in ("pending", "running"):  # never finished
                    event.status = "interrupted" if turn.status == "interrupted" else "incomplete"
            replies = [e for e in in_turn if e.kind == model.AGENT_MESSAGE]
            if replies and turn.status == "completed":
                replies[-1].payload["phase"] = "final"  # opencode doesn't label its final answer
        main = [t for t in turns if t.thread_id == self.session.id]
        self.session.outcome = main[-1].status if main else None
        return NormalizedSession(self.session, self.threads, turns, self.events)


def _tool_call(part: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = part.get("tool")
    args = (part.get("state") or {}).get("input") or {}
    base = {"source_type": name, "source_id": part.get("callID")}
    if name == "bash":
        return model.COMMAND, {**base, "command": args.get("command"), "description": args.get("description"),
                                     "exit_code": None, "output": None, "actions": []}
    if name in FILE_TOOLS:
        path = args.get("filePath")
        return model.FILE_CHANGE, {**base, "tool": name, "paths": [path] if path else []}
    if name in READ_TOOLS:
        target = args.get("filePath") or args.get("path")
        query = args.get("pattern")
        return model.TOOL_CALL, {**base, "tool": name, "arguments": args, "actions": [
            {"type": READ_TOOLS[name], "path": target, "command": f"{name} {query}" if query else None}]}
    if name in SUBAGENT_TOOLS:
        return model.SUBAGENT_CALL, {**base, "action": "spawn", "prompt": args.get("prompt"),
                                           "description": args.get("description"),
                                           "agent_type": args.get("subagent_type"), "receiver_thread_ids": []}
    return model.TOOL_CALL, {**base, "tool": name, "server": None, "arguments": args}


def _status(state: dict[str, Any]) -> str:
    status = state.get("status")
    if status == "error":
        error = str(state.get("error") or "")
        if any(marker in error for marker in DECLINED):
            return "declined"
        if "aborted" in error.lower():
            return "interrupted"
        return "failed"
    return status or "incomplete"  # completed, or pending/running (settled in _finish)


def _file_changes(tool: str | None, state: dict[str, Any]) -> list[FileChange]:
    args = state.get("input") or {}
    metadata = state.get("metadata") or {}
    path = args.get("filePath") or metadata.get("filepath")
    if tool == "write" and metadata.get("exists") is False and path:
        return [FileChange(path, model.ADD, args.get("content") or "")]
    filediff = metadata.get("filediff") or {}
    if path and isinstance(filediff.get("before"), str) and isinstance(filediff.get("after"), str):
        return [FileChange(path, model.UPDATE, _diff_contents(filediff["before"], filediff["after"]))]
    # Newer versions keep the full patch instead of both contents; `diff` is for display (common
    # indentation stripped), so only a fallback.
    hunks = _hunks(filediff.get("patch")) or _hunks(metadata.get("diff"))
    if hunks and path:
        return [FileChange(path, model.UPDATE, hunks)]
    if tool == "write" and path and args.get("content") is not None:
        return [FileChange(path, model.ADD, args["content"])]  # an overwrite with no diff recorded
    return []  # e.g. a patch tool whose format isn't known yet: workspace observation still sees it


def _hunks(diff: Any) -> str | None:
    """opencode's unified diff (with `Index:`/`---`/`+++` headers) reduced to its hunks."""
    if not isinstance(diff, str) or "@@" not in diff:
        return None
    lines = diff.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("@@"))
    return "\n".join(line for line in lines[first:] if not line.startswith("\\")) + "\n"


def _diff_contents(before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++"))) + "\n"


def _json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
