"""Canonical, agent-agnostic event model.

Adapters translate an agent's native records into these types. Nothing here may
mention a specific agent: if a field only makes sense for one agent, it belongs
in an event's payload, not in the model.

Timestamps are integer milliseconds since the Unix epoch (UTC).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# What an event is. Adapters must map every native record to one of these;
# anything unrecognised becomes TOOL_CALL with its raw payload, never dropped.
USER_MESSAGE = "user_message"
AGENT_MESSAGE = "agent_message"
REASONING = "reasoning"
COMMAND = "command"
FILE_CHANGE = "file_change"
TOOL_CALL = "tool_call"
APPROVAL = "approval"
SUBAGENT_CALL = "subagent_call"
TOKEN_USAGE = "token_usage"
ERROR = "error"
KINDS = (USER_MESSAGE, AGENT_MESSAGE, REASONING, COMMAND, FILE_CHANGE, TOOL_CALL,
         APPROVAL, SUBAGENT_CALL, TOKEN_USAGE, ERROR)

# File change kinds.
ADD, UPDATE, DELETE, MOVE = "add", "update", "delete", "move"

# What a command does, as far as the agent can tell (payload["actions"][i]["type"]).
READ, LIST, SEARCH, OTHER = "read", "list", "search", "other"


@dataclass
class Session:
    """One recorded run of an agent: a tree of threads."""
    id: str
    agent: str
    agent_version: str | None
    cwd: str | None
    started_at: int | None
    ended_at: int | None
    outcome: str | None
    source: str  # where the raw records came from (capture directory, log file, ...)


@dataclass
class Thread:
    """One agent conversation. Sub-agents are child threads."""
    id: str
    session_id: str
    parent_thread_id: str | None = None


@dataclass
class Turn:
    """One user request and everything the agent did to answer it."""
    id: str
    thread_id: str
    seq: int  # 1-based position within the thread
    input_text: str | None = None
    status: str | None = None
    started_at: int | None = None
    ended_at: int | None = None


@dataclass
class FileChange:
    path: str  # absolute
    kind: str  # ADD | UPDATE | DELETE | MOVE
    diff: str | None  # full content for ADD, unified hunks for UPDATE/MOVE
    move_path: str | None = None  # destination for MOVE


@dataclass
class Event:
    """One thing the agent (or its user) did."""
    id: str
    session_id: str
    thread_id: str | None
    turn_id: str | None
    seq: int  # 1-based order within the session
    kind: str
    status: str | None
    started_at: int | None
    ended_at: int | None
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None  # e.g. the command an approval gates
    source_lines: list[int] = field(default_factory=list)  # 1-based lines in the raw source
    file_changes: list[FileChange] = field(default_factory=list)


@dataclass
class NormalizedSession:
    session: Session
    threads: list[Thread]
    turns: list[Turn]
    events: list[Event]
