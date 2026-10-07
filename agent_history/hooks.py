"""`ah hook <agent> <pre|post|stop>`: observe the workspace around an agent's tool calls.

Agents run this before and after every tool call that can change files, passing a JSON
description of the call on stdin. Around each call the workspace is snapshotted, so the
difference is exactly what that call changed, whether it used an edit tool or the shell.

  pre   anything that changed since the last snapshot happened outside any agent (a
        person editing, another program); then an observation opens for this call
  post  the changes since `pre` are this call's; its observation closes
  stop  the agent's turn ended (or a new prompt arrived): calls still open will never get
        their `post` (interrupted), so close them

Observations that overlap in one workspace (parallel tool calls, two agents at once) are
marked concurrent: a snapshot can't tell which of them made a change.

A hook must never disturb the agent: it always exits 0, never writes to stdout, and logs
failures to hooks.log next to the database.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from agent_history import workspace
from agent_history.store import Store

EVENTS = ("pre", "post", "stop")


@dataclass
class HookCall:
    agent: str
    session_id: str | None
    tool_call_id: str | None
    tool_name: str | None
    command: str | None  # what the call ran or touched, for display
    cwd: str


def _claude_compatible(agent: str) -> Callable[[dict[str, Any]], HookCall]:
    """Claude Code's hook input; Codex uses the same fields (`session_id`, `tool_use_id`, ...)."""
    def parse(payload: dict[str, Any]) -> HookCall:
        tool_input = payload.get("tool_input")
        command = None
        if isinstance(tool_input, dict):
            command = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("notebook_path")
        elif isinstance(tool_input, str):
            command = tool_input
        return HookCall(agent, payload.get("session_id"), payload.get("tool_use_id"), payload.get("tool_name"),
                        command if isinstance(command, str) else None, payload.get("cwd") or os.getcwd())
    return parse


# The only place hook formats are tied to agents.
PARSERS: dict[str, Callable[[dict[str, Any]], HookCall]] = {
    "claude-code": _claude_compatible("claude-code"),
    "codex": _claude_compatible("codex"),
}


def handle(store: Store, agent: str, event: str, payload: dict[str, Any], now: int | None = None) -> None:
    """Record what changed in the workspace of this hook call. `now` is in milliseconds."""
    call = PARSERS[agent](payload)
    now = now if now is not None else int(time.time() * 1000)
    root = str(workspace.root_of(call.cwd))
    since = store.last_observed(root)
    baseline, changes = workspace.snapshot(store, Path(root))
    if baseline:
        store.add_observation(root, "baseline", now, now, changes)
        changes = []
    running = store.open_observations(root)
    mine = [o for o in running if (o["agent_session_id"], o["tool_call_id"]) == (call.session_id, call.tool_call_id)]
    others = [o for o in running if o not in mine]
    details = {"agent": call.agent, "agent_session_id": call.session_id, "tool_call_id": call.tool_call_id,
               "tool_name": call.tool_name, "command": call.command}

    if event == "pre":
        if changes and others:  # another call is still running: it's the likely author
            store.add_changes(others[-1]["id"], changes)
        elif changes:
            store.add_observation(root, "outside", since or now, now, changes)
        if not mine:
            mine = [{"id": store.add_observation(root, "agent", now, None, **details)}]
    elif event == "post":
        if mine:
            store.close_observation(mine[-1]["id"], now, changes)
        else:  # no `pre` was seen for this call (hooks installed mid-call, or `pre` failed)
            mine = [{"id": store.add_observation(root, "agent", now, now, changes, **details)}]
    elif event == "stop":
        leftover = [o for o in running if o["agent_session_id"] == call.session_id]
        if changes and leftover:
            store.add_changes(leftover[-1]["id"], changes)
        elif changes:
            store.add_observation(root, "outside", since or now, now, changes)
        for observation in leftover:
            store.close_observation(observation["id"], now, [])
        return
    if others:
        store.mark_concurrent([o["id"] for o in mine] + [o["id"] for o in others])


def run(agent: str, event: str, stdin: str, db_path: str | Path) -> int:
    """Entry point for `ah hook`. Never fails and never prints: errors go to hooks.log."""
    db_path = Path(db_path)
    try:
        if agent not in PARSERS or event not in EVENTS:
            raise ValueError(f"unknown hook {agent} {event}")
        payload = json.loads(stdin or "{}")
        with _lock(db_path.parent / "hooks.lock"):
            store = Store(db_path)
            try:
                started = time.monotonic()
                handle(store, agent, event, payload if isinstance(payload, dict) else {})
                elapsed = time.monotonic() - started
                if elapsed > 1:
                    _log(db_path, f"slow hook: {agent} {event} took {elapsed:.2f}s")
            finally:
                store.close()
    except Exception:  # a broken hook must never break the agent
        _log(db_path, f"{agent} {event} failed:\n{traceback.format_exc()}")
    return 0


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    """Serialize hooks from parallel tool calls, so each change is recorded exactly once."""
    try:
        import fcntl
    except ImportError:  # not on Windows; SQLite's own locking still applies
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _log(db_path: Path, message: str) -> None:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with (db_path.parent / "hooks.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass
