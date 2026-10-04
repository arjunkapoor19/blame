"""Codex session logs (`~/.codex/sessions/**/rollout-*.jsonl`) -> canonical events.

Codex writes one of these for every session, however it was started (TUI, exec,
IDE, App Server), so history is available without running Codex through the
recorder. Only `event_msg` records are read; their `item_completed` items mirror
the App Server's items, so each one is converted to that shape and translated by
the App Server adapter, keeping a single Codex -> canonical mapping.

Compared with a recorder capture, logs have no approvals and no record of a
command that was still running when its turn was interrupted.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from agent_history import model
from agent_history.adapters.codex import AGENT, _item_status, _iso_ms, _parse, _translate
from agent_history.model import Event, NormalizedSession, Session, Thread, Turn

PARSED_ACTIONS = {"read": "read", "list_files": "listFiles", "search": "search"}


def normalize(path: str | Path) -> NormalizedSession:
    path = Path(path).resolve()
    with path.open(encoding="utf-8", errors="replace") as lines:
        records = list(_parse(lines))
    meta = next((r.get("payload") or {} for _, r in records if r.get("type") == "session_meta"), {})
    session_id = meta.get("id") or meta.get("session_id") or path.stem
    session = Session(id=session_id, agent=AGENT, agent_version=meta.get("cli_version"), cwd=meta.get("cwd"),
                      started_at=_iso_ms(meta.get("timestamp")), ended_at=None, outcome=None, source=str(path))
    threads = {session_id: Thread(session_id, session_id)}
    turns: dict[str, Turn] = {}
    events: list[Event] = []
    current_turn: str | None = None

    for line, record in records:
        timestamp = _iso_ms(record.get("timestamp"))
        if timestamp is not None:
            session.ended_at = timestamp
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload") or {}
        kind = payload.get("type")
        if kind == "task_started":
            current_turn = payload.get("turn_id")
            if current_turn and current_turn not in turns:
                turns[current_turn] = Turn(current_turn, session_id, len(turns) + 1, status="in_progress",
                                           started_at=timestamp)
        elif kind in ("task_complete", "turn_aborted"):
            turn = turns.get(payload.get("turn_id"))
            if turn is not None:
                turn.status = "completed" if kind == "task_complete" else payload.get("reason") or "interrupted"
                turn.ended_at = timestamp
        elif kind == "item_completed":
            item = _to_app_server_item(payload.get("item") or {})
            event_kind, event_payload, changes = _translate(item)
            thread_id = payload.get("thread_id") or session_id
            threads.setdefault(thread_id, Thread(thread_id, session_id))
            duration = event_payload.get("duration_ms") if event_kind == model.COMMAND else None
            started = timestamp - duration if timestamp is not None and duration else timestamp
            turn_id = payload.get("turn_id") or current_turn
            turn = turns.get(turn_id)
            status = _item_status(item, completed=True)
            if turn is not None and turn.status not in ("in_progress", "completed") and event_kind == model.COMMAND:
                status = turn.status  # killed by the abort; Codex logs it as failed with exit code -1
            events.append(Event(
                id=f"{session_id}:{len(events) + 1}", session_id=session_id, thread_id=thread_id, turn_id=turn_id,
                seq=len(events) + 1, kind=event_kind, status=status,
                started_at=started, ended_at=timestamp, payload=event_payload, source_lines=[line],
                file_changes=changes))
            if event_kind == model.USER_MESSAGE and turn is not None and turn.input_text is None:
                turn.input_text = event_payload["text"]
        elif kind == "token_count":
            info = payload.get("info") or {}
            usage = {"total": _tokens(info.get("total_token_usage")), "last": _tokens(info.get("last_token_usage")),
                     "context_window": info.get("model_context_window")}
            events.append(Event(
                id=f"{session_id}:{len(events) + 1}", session_id=session_id, thread_id=session_id,
                turn_id=current_turn, seq=len(events) + 1, kind=model.TOKEN_USAGE, status=None,
                started_at=timestamp, ended_at=timestamp, payload=usage, source_lines=[line]))

    for turn in turns.values():
        if turn.status == "in_progress":  # the log ends mid-turn: still running, or Codex stopped
            turn.status = "incomplete"
    ordered = sorted(turns.values(), key=lambda t: t.seq)
    session.outcome = ordered[-1].status if ordered else None
    return NormalizedSession(session, list(threads.values()), ordered, events)


def _to_app_server_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a log item to the App Server item shape `_translate` understands."""
    native = item.get("type")
    base = {"id": item.get("id"), "status": item.get("status")}
    if native == "UserMessage":
        return {**base, "type": "userMessage", "content": item.get("content") or []}
    if native == "AgentMessage":
        texts = [part.get("text", "") for part in item.get("content") or []
                 if str(part.get("type")).lower() in ("text", "output_text")]
        return {**base, "type": "agentMessage", "text": "".join(texts), "phase": item.get("phase")}
    if native == "Reasoning":
        return {**base, "type": "reasoning", "summary": item.get("summary_text") or [],
                "content": item.get("raw_content") or []}
    if native == "CommandExecution":
        argv = item.get("command")
        duration = item.get("duration") or {}
        return {
            **base, "type": "commandExecution",
            "command": shlex.join(argv) if isinstance(argv, list) else argv,
            "cwd": _strip_file_url(item.get("cwd")),
            "exitCode": item.get("exit_code"),
            "aggregatedOutput": item.get("aggregated_output"),
            "durationMs": (duration.get("secs", 0) * 1000 + duration.get("nanos", 0) // 1_000_000)
            if duration else None,
            "commandActions": [{"type": PARSED_ACTIONS.get(a.get("type"), "unknown"), "path": a.get("path"),
                                "command": a.get("cmd")} for a in item.get("parsed_cmd") or []],
        }
    if native == "FileChange":
        changes = [{"path": path, "kind": {"type": change.get("type"), "move_path": change.get("move_path")},
                    "diff": change.get("unified_diff") if change.get("unified_diff") is not None
                    else change.get("content")}
                   for path, change in (item.get("changes") or {}).items()]
        return {**base, "type": "fileChange", "changes": changes}
    if native == "Extension" and item.get("kind") == "web.search":
        return {**base, "type": "webSearch", "query": item.get("query"), "results": item.get("results")}
    return item  # unknown: the translator keeps it whole as a tool_call


def _tokens(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    return {"input": usage.get("input_tokens"), "cached_input": usage.get("cached_input_tokens"),
            "output": usage.get("output_tokens"), "reasoning_output": usage.get("reasoning_output_tokens"),
            "total": usage.get("total_tokens")}


def _strip_file_url(value: str | None) -> str | None:
    return value[len("file://"):] if value and value.startswith("file://") else value
