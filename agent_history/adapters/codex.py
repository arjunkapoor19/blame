"""Codex App Server captures -> canonical events.

Input is a capture directory written by recorder/codex_recorder.py:

  events.jsonl  raw server->client JSON-RPC (source of truth; source_lines point here)
  wire.jsonl    both directions; used only to recover how approvals were answered
  session.json  recorder metadata (optional)

What is kept and what is dropped is documented in docs/event-model.md.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_history import model
from agent_history.model import Event, FileChange, NormalizedSession, Session, Thread, Turn

AGENT = "codex"

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}
APPROVE_DECISIONS = {"accept", "acceptForSession", "approved", "approved_for_session"}
DECLINE_DECISIONS = {"decline", "denied", "cancel", "abort"}

TOOL_ITEMS = {"mcpToolCall", "dynamicToolCall", "functionCallOutput", "webSearch"}
SUBAGENT_ITEMS = {"collabAgentToolCall", "subAgentActivity"}

ACTIONS = {"read": model.READ, "listFiles": model.LIST, "search": model.SEARCH}
STATUSES = {"inProgress": "in_progress", "completed": "completed", "failed": "failed",
            "declined": "declined", "interrupted": "interrupted"}


def normalize(capture_dir: str | Path) -> NormalizedSession:
    directory = Path(capture_dir).resolve()
    meta = _read_json(directory / "session.json") or {}
    with (directory / "events.jsonl").open(encoding="utf-8", errors="replace") as lines:
        messages = list(_parse(lines))
    return _Normalizer(directory, meta, _approval_decisions(directory / "wire.jsonl")).run(messages)


class _Normalizer:
    def __init__(self, directory: Path, meta: dict[str, Any], decisions: dict[Any, str]) -> None:
        self.meta = meta
        self.decisions = decisions
        codex = meta.get("codex") or {}
        settings = meta.get("settings") or {}
        version = codex.get("version")
        self.session = Session(
            id=directory.name,
            agent=AGENT,
            agent_version=version.split()[-1] if version else None,
            cwd=settings.get("cwd"),
            started_at=_iso_ms(meta.get("started_at")),
            ended_at=_iso_ms(meta.get("ended_at")),
            outcome=meta.get("outcome"),
            source=str(directory),
        )
        self.threads: dict[str, Thread] = {}
        self.turns: dict[str, Turn] = {}
        self.events: list[Event] = []
        self.by_item: dict[str, Event] = {}  # native item id -> event
        self.open_items: dict[str, Event] = {}

    def run(self, messages: list[tuple[int, dict[str, Any]]]) -> NormalizedSession:
        for line, message in messages:
            method = message.get("method")
            params = message.get("params") or {}
            if method is None:
                self._response(message)
            elif "id" in message:
                self._server_request(line, method, message["id"], params)
            else:
                self._notification(line, method, params, message.get("emittedAtMs"))
        for event in self.open_items.values():  # the stream ended mid-item
            event.status = "incomplete"
        return NormalizedSession(self.session, list(self.threads.values()),
                                 list(self.turns.values()), self.events)

    # -- message shapes -------------------------------------------------------

    def _response(self, message: dict[str, Any]) -> None:
        # Responses are plumbing, except thread/start's, which names the root thread.
        thread = (message.get("result") or {}).get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            self._thread(thread["id"], thread.get("parentThreadId"))
            if self.session.cwd is None:
                self.session.cwd = thread.get("cwd")

    def _server_request(self, line: int, method: str, request_id: Any, params: dict[str, Any]) -> None:
        if method not in APPROVAL_METHODS:
            return
        item_id = params.get("itemId") or params.get("callId")
        target = self.by_item.get(item_id) if item_id else None
        decision = self.decisions.get(request_id)
        if decision in APPROVE_DECISIONS:
            status = "approved"
        elif decision in DECLINE_DECISIONS:
            status = "declined"
        else:
            status = decision
        payload = {"source_type": method, "decision": decision,
                   "subject": target.kind if target else None,
                   "command": _unwrap_shell(params.get("command")), "reason": params.get("reason")}
        ts = params.get("startedAtMs")
        self._add(line, model.APPROVAL, status, ts, ts, payload,
                  thread_id=params.get("threadId") or (target.thread_id if target else None),
                  turn_id=params.get("turnId") or (target.turn_id if target else None),
                  parent_id=target.id if target else None)

    def _notification(self, line: int, method: str, params: dict[str, Any], emitted: int | None) -> None:
        if method == "thread/started":
            thread = params.get("thread") or {}
            self._thread(thread.get("id"), thread.get("parentThreadId"))
        elif method == "turn/started":
            self._turn_started(params, emitted)
        elif method == "turn/completed":
            self._turn_completed(params, emitted)
        elif method == "item/started":
            self._item(line, params, completed=False)
        elif method == "item/completed":
            self._item(line, params, completed=True)
        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            payload = {"total": _tokens(usage.get("total")), "last": _tokens(usage.get("last")),
                       "context_window": usage.get("modelContextWindow")}
            self._add(line, model.TOKEN_USAGE, None, emitted, emitted, payload,
                      thread_id=params.get("threadId"), turn_id=params.get("turnId"))
        elif method == "error":
            error = params.get("error") or {}
            payload = {"message": error.get("message"), "will_retry": params.get("willRetry"),
                       "details": error}
            self._add(line, model.ERROR, None, emitted, emitted, payload,
                      thread_id=params.get("threadId"), turn_id=params.get("turnId"))
        # Everything else is plumbing or streaming (see docs/event-model.md).

    # -- threads and turns ----------------------------------------------------

    def _thread(self, thread_id: str | None, parent_id: str | None = None) -> None:
        if not thread_id:
            return
        thread = self.threads.get(thread_id)
        if thread is None:
            self.threads[thread_id] = Thread(thread_id, self.session.id, parent_id)
        elif parent_id and not thread.parent_thread_id:
            thread.parent_thread_id = parent_id

    def _turn_started(self, params: dict[str, Any], emitted: int | None) -> None:
        thread_id = params.get("threadId")
        turn_id = (params.get("turn") or {}).get("id")
        if not turn_id or turn_id in self.turns:
            return
        self._thread(thread_id)
        seq = 1 + sum(1 for turn in self.turns.values() if turn.thread_id == thread_id)
        self.turns[turn_id] = Turn(turn_id, thread_id, seq, status="in_progress", started_at=emitted)

    def _turn_completed(self, params: dict[str, Any], emitted: int | None) -> None:
        native = params.get("turn") or {}
        turn = self.turns.get(native.get("id"))
        if turn is None:
            return
        turn.status = STATUSES.get(native.get("status"), native.get("status"))
        turn.ended_at = emitted
        # Items the turn never completed (e.g. a command killed by turn/interrupt).
        for item_id, event in list(self.open_items.items()):
            if event.turn_id == turn.id:
                event.status = "interrupted" if turn.status == "interrupted" else "incomplete"
                event.ended_at = emitted
                del self.open_items[item_id]

    # -- items ----------------------------------------------------------------

    def _item(self, line: int, params: dict[str, Any], completed: bool) -> None:
        item = params.get("item") or {}
        item_id = item.get("id")
        kind, payload, changes = _translate(item)
        status = _item_status(item, completed)
        event = self.by_item.get(item_id) if item_id else None
        if event is None:
            event = self._add(line, kind, status, params.get("startedAtMs") or params.get("completedAtMs"),
                              None, payload, thread_id=params.get("threadId"), turn_id=params.get("turnId"))
            if item_id:
                self.by_item[item_id] = event
        else:
            event.source_lines.append(line)
            event.status, event.payload = status, payload
        event.file_changes = changes
        if completed:
            event.ended_at = params.get("completedAtMs")
            self.open_items.pop(item_id, None)
        elif item_id:
            self.open_items[item_id] = event

        if kind == model.USER_MESSAGE and completed:
            turn = self.turns.get(event.turn_id)
            if turn is not None and turn.input_text is None:
                turn.input_text = payload["text"]
        if item.get("type") == "collabAgentToolCall":
            for receiver in item.get("receiverThreadIds") or []:
                self._thread(receiver, item.get("senderThreadId") or event.thread_id)

    def _add(self, line: int, kind: str, status: str | None, started: int | None, ended: int | None,
             payload: dict[str, Any], thread_id: str | None, turn_id: str | None,
             parent_id: str | None = None) -> Event:
        self._thread(thread_id)
        seq = len(self.events) + 1
        event = Event(id=f"{self.session.id}:{seq}", session_id=self.session.id, thread_id=thread_id,
                      turn_id=turn_id, seq=seq, kind=kind, status=status, started_at=started,
                      ended_at=ended, payload=payload, parent_id=parent_id, source_lines=[line])
        self.events.append(event)
        return event


def _translate(item: dict[str, Any]) -> tuple[str, dict[str, Any], list[FileChange]]:
    """Map one native item to (kind, payload, file changes)."""
    native = item.get("type")
    payload: dict[str, Any] = {"source_type": native, "source_id": item.get("id")}
    if native == "userMessage":
        texts = [part.get("text", "") for part in item.get("content") or [] if part.get("type") == "text"]
        payload["text"] = "\n".join(texts)
        return model.USER_MESSAGE, payload, []
    if native == "agentMessage":
        phase = item.get("phase")
        payload.update(text=item.get("text") or "", phase="final" if phase == "final_answer" else phase)
        return model.AGENT_MESSAGE, payload, []
    if native == "plan":
        payload.update(text=item.get("text") or "", phase="plan")
        return model.AGENT_MESSAGE, payload, []
    if native == "reasoning":
        payload.update(summary=item.get("summary") or [], content=item.get("content") or [])
        return model.REASONING, payload, []
    if native == "commandExecution":
        payload.update(
            command=_unwrap_shell(item.get("command")),
            raw_command=item.get("command"),
            cwd=item.get("cwd"),
            exit_code=item.get("exitCode"),
            output=item.get("aggregatedOutput"),
            duration_ms=item.get("durationMs"),
            actions=[{"type": ACTIONS.get(a.get("type"), model.OTHER), "path": a.get("path"),
                      "command": a.get("command")} for a in item.get("commandActions") or []],
        )
        return model.COMMAND, payload, []
    if native == "fileChange":
        changes = [_file_change(change) for change in item.get("changes") or []]
        payload["paths"] = [change.path for change in changes]
        return model.FILE_CHANGE, payload, changes
    if native in SUBAGENT_ITEMS:
        payload.update(
            action=item.get("tool") or item.get("kind"),
            prompt=item.get("prompt"),
            model=item.get("model"),
            sender_thread_id=item.get("senderThreadId"),
            receiver_thread_ids=item.get("receiverThreadIds") or
            ([item["agentThreadId"]] if item.get("agentThreadId") else []),
        )
        return model.SUBAGENT_CALL, payload, []
    if native in TOOL_ITEMS:
        payload.update(
            tool="web_search" if native == "webSearch" else item.get("tool") or item.get("name"),
            server=item.get("server") or item.get("namespace"),
            arguments=item.get("arguments") if native != "webSearch" else {"query": item.get("query")},
            result=item.get("result") or item.get("contentItems") or item.get("output") or item.get("results"),
            error=item.get("error"),
            duration_ms=item.get("durationMs"),
        )
        return model.TOOL_CALL, payload, []
    # Unknown or not-yet-modelled item: keep everything rather than lose it.
    payload.update(tool=native, raw=item)
    return model.TOOL_CALL, payload, []


def _file_change(change: dict[str, Any]) -> FileChange:
    kind = (change.get("kind") or {}).get("type")
    move_path = (change.get("kind") or {}).get("move_path")
    if kind == "update" and move_path:
        kind = model.MOVE
    elif kind not in (model.ADD, model.UPDATE, model.DELETE):
        kind = model.UPDATE
    return FileChange(path=change.get("path"), kind=kind, diff=change.get("diff"), move_path=move_path)


def _item_status(item: dict[str, Any], completed: bool) -> str | None:
    native = item.get("status")
    if native is not None:
        return STATUSES.get(native, native)
    return "completed" if completed else "in_progress"


def _unwrap_shell(command: str | None) -> str | None:
    """`/bin/zsh -lc 'cat x'` -> `cat x`. Anything else is returned unchanged."""
    if not command:
        return command
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if len(argv) == 3 and argv[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh"} and argv[1] in {"-c", "-lc"}:
        return argv[2]
    return command


def _tokens(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    return {"input": usage.get("inputTokens"), "cached_input": usage.get("cachedInputTokens"),
            "output": usage.get("outputTokens"), "reasoning_output": usage.get("reasoningOutputTokens"),
            "total": usage.get("totalTokens")}


def _approval_decisions(wire_path: Path) -> dict[Any, str]:
    """Server request id -> decision we sent, from the client side of wire.jsonl."""
    decisions: dict[Any, str] = {}
    if not wire_path.exists():
        return decisions
    with wire_path.open(encoding="utf-8", errors="replace") as lines:
        for _, entry in _parse(lines):
            if entry.get("dir") != "send":
                continue
            try:
                message = json.loads(entry.get("raw") or "")
            except ValueError:
                continue
            # The client only ever sends `result` when answering a server request.
            if isinstance(message, dict) and isinstance(message.get("result"), dict) and "id" in message:
                decision = message["result"].get("decision")
                if decision is not None:
                    decisions[message["id"]] = decision if isinstance(decision, str) else json.dumps(decision)
    return decisions


def _parse(lines: Any) -> Any:
    for number, raw in enumerate(lines, start=1):
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if isinstance(message, dict):
            yield number, message


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
