"""What an event or a line's author amounts to, independent of how it is shown.

The terminal (`cli.py`) and the viewer (`view.py`) both render these, so they always
agree on what a step is: a test or a command, a read or a tool call, and how it went.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent_history import model
from agent_history.blame import Author
from agent_history.commands import is_test, read_only_label, summarize
from agent_history.model import Event

SIGNS = {model.ADD: "+", model.DELETE: "-", model.UPDATE: "~", model.MOVE: "→"}


@dataclass
class Outline:
    """One step, classified. `label` is what the step is (user, agent, answer, read, test,
    command, edit, approval, tool, subagent, error, thinking); `title` is its full text."""
    label: str
    title: str
    status: str | None = None
    summary: str | None = None  # e.g. a test verdict, or the prompt handed to a sub-agent


def outline(event: Event, cwd: str | None = None) -> Outline | None:
    """The step an event is, or None if it isn't worth a line in a timeline."""
    p = event.payload
    if event.kind == model.USER_MESSAGE:
        return Outline("user", p.get("text") or "")
    if event.kind == model.AGENT_MESSAGE:
        return Outline("answer" if p.get("phase") == "final" else "agent", p.get("text") or "")
    if event.kind == model.COMMAND:
        reads = read_only_label(p) if event.status == "completed" else None
        if reads:
            return Outline("read", reads, event.status)
        summary = summarize(p, event.status)
        if summary is None and p.get("exit_code") not in (0, None):
            summary = f"exit {p['exit_code']}"
        return Outline("test" if is_test(p.get("command")) else "command", p.get("command") or "", event.status,
                       summary)
    if event.kind == model.FILE_CHANGE:
        files = ", ".join(f"{SIGNS.get(c.kind, '~')}{relative(c.path, cwd)}" for c in event.file_changes)
        return Outline("edit", files, event.status)
    if event.kind == model.APPROVAL:
        return Outline("approval", p.get("command") or p.get("subject") or "", event.status)
    if event.kind == model.TOOL_CALL:
        reads = read_only_label(p) if event.status == "completed" else None
        if reads:
            return Outline("read", reads, event.status)
        return Outline("tool", "/".join(str(x) for x in (p.get("server"), p.get("tool")) if x), event.status)
    if event.kind == model.SUBAGENT_CALL:
        receivers = ", ".join(t[-8:] for t in p.get("receiver_thread_ids") or [])
        return Outline("subagent", f"{p.get('action')}{' → ' + receivers if receivers else ''}", event.status,
                       p.get("description") or p.get("prompt"))
    if event.kind == model.ERROR:
        return Outline("error", p.get("message") or "", event.status)
    if event.kind == model.REASONING and p.get("summary"):
        return Outline("thinking", " ".join(map(str, p["summary"])))
    return None


def author_name(author: Author | None) -> str:
    """The agent's name up to its first `-`, `you` for edits outside any agent, `·` for pre-existing lines."""
    if author is None:
        return ""
    if author.kind == "outside":
        return "you"
    if author.kind == "baseline":
        return "·"
    return (author.agent or "agent").split("-")[0]


def relative(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return os.path.relpath(path, cwd)
    return path
