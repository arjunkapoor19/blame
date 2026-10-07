"""Claude Code session transcripts (`~/.claude/projects/<project>/<session-id>.jsonl`) -> canonical events.

Claude Code writes one JSON record per line while it works. The records used here:

  user       the person's prompt (starts a turn; records in a turn share `promptId`), or
             `tool_result` blocks answering the agent's tool calls, with a structured
             `toolUseResult` (Edit/Write include line-numbered hunks: `structuredPatch`)
  assistant  content blocks: `text`, `thinking`, `tool_use`. One API reply is split across
             several records that repeat the same `message.usage`; it is counted once.

Everything else (attachments, mode changes, titles, file-history snapshots, ...) is
bookkeeping, not agent behaviour. What is kept and why is in docs/event-model.md.

Resuming a conversation starts a new transcript that copies the earlier records (same
`uuid`, new `sessionId`). So the thread is identified by the conversation's first record,
which every copy shares: the store then keeps one copy of the run, the most recent.
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_history import model
from agent_history.model import Event, FileChange, NormalizedSession, Session, Thread, Turn

AGENT = "claude-code"

FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read": model.READ, "NotebookRead": model.READ, "Grep": model.SEARCH,
              "Glob": model.LIST, "LS": model.LIST}
SUBAGENT_TOOLS = {"Task", "Agent"}

NOT_PROMPTS = ("<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-caveat>")
INTERRUPTED = "[Request interrupted by user"
DECLINED = ("The user doesn't want to proceed", "User rejected tool use")
EXIT_CODE = re.compile(r"^(?:Error: )?Exit code (-?\d+)")


def normalize(path: str | Path) -> NormalizedSession:
    return _Normalizer(Path(path).resolve()).run()


def looks_like_transcript(path: Path) -> bool:
    """A Claude Code transcript: JSON lines carrying `sessionId` and a user/assistant `type`."""
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for _, record in zip(range(50), _records(lines)):
                if record[1].get("sessionId") and record[1].get("type") in ("user", "assistant"):
                    return True
    except OSError:
        pass
    return False


class _Normalizer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.session: Session | None = None
        self.thread_id: str | None = None  # the uuid of the conversation's first record
        self.turns: dict[str, Turn] = {}
        self.turn: Turn | None = None  # the turn new records belong to
        self.events: list[Event] = []
        self.pending: dict[str, Event] = {}  # tool_use id -> event awaiting its result
        self.counted: set[str] = set()  # API message ids whose usage has been counted
        self.totals: dict[str, int] = {}
        self.stop_reason: dict[str, str | None] = {}  # turn id -> how its last reply ended
        self.interrupted: set[str] = set()

    def run(self) -> NormalizedSession:
        with self.path.open(encoding="utf-8", errors="replace") as lines:
            for line, record in _records(lines):
                if self.session is None and record.get("sessionId"):
                    self.session = Session(
                        id=record["sessionId"], agent=AGENT, agent_version=record.get("version"),
                        cwd=record.get("cwd"), started_at=None, ended_at=None, outcome=None,
                        source=str(self.path))
                if self.session is None:
                    continue
                # Bookkeeping records come first and lack these; take them from the first that has them.
                self.session.agent_version = self.session.agent_version or record.get("version")
                self.session.cwd = self.session.cwd or record.get("cwd")
                timestamp = _ms(record.get("timestamp"))
                if timestamp is not None:
                    self.session.started_at = self.session.started_at or timestamp
                    self.session.ended_at = timestamp
                if self.thread_id is None and record.get("type") in ("user", "assistant") and record.get("uuid"):
                    self.thread_id = record["uuid"]
                if record.get("type") == "user":
                    self._user(line, record, timestamp)
                elif record.get("type") == "assistant":
                    self._assistant(line, record, timestamp)
        if self.session is None:
            raise ValueError(f"{self.path}: no Claude Code records")
        return self._finish()

    # -- records ----------------------------------------------------------------

    def _user(self, line: int, record: dict[str, Any], timestamp: int | None) -> None:
        content = (record.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            for block in results:
                self._result(line, record, block, timestamp)
            return
        text = content if isinstance(content, str) else "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if record.get("isMeta") or record.get("isCompactSummary") or text.lstrip().startswith(NOT_PROMPTS):
            return  # slash commands, their output, and summaries Claude Code injects
        if text.lstrip().startswith(INTERRUPTED):
            turn = self.turns.get(record.get("promptId")) or self.turn
            if turn is not None:
                self.interrupted.add(turn.id)
            return
        prompt_id = record.get("promptId") or record.get("uuid")
        if prompt_id not in self.turns:
            self.turns[prompt_id] = Turn(prompt_id, self.thread_id or self.session.id, len(self.turns) + 1,
                                         input_text=text, started_at=timestamp)
        self.turn = self.turns[prompt_id]
        self._add(line, record, model.USER_MESSAGE, "completed", timestamp, {"text": text})

    def _assistant(self, line: int, record: dict[str, Any], timestamp: int | None) -> None:
        message = record.get("message") or {}
        if self.turn is not None and message.get("stop_reason") is not None:
            self.stop_reason[self.turn.id] = message["stop_reason"]
        message_id = message.get("id")
        if message.get("usage") and message_id not in self.counted:
            self.counted.add(message_id)
            self._add(line, record, model.TOKEN_USAGE, None, timestamp, self._usage(message))
        for block in message.get("content") or []:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text" and block.get("text", "").strip():
                self._add(line, record, model.AGENT_MESSAGE, "completed", timestamp,
                          {"text": block["text"], "phase": "commentary", "model": message.get("model")})
            elif kind == "thinking" and block.get("thinking", "").strip():
                self._add(line, record, model.REASONING, "completed", timestamp,
                          {"summary": [], "content": [block["thinking"]]})
            elif kind == "tool_use":
                tool_kind, payload = _tool_call(block)
                self.pending[block.get("id")] = self._add(line, record, tool_kind, "in_progress", timestamp, payload)

    def _result(self, line: int, record: dict[str, Any], block: dict[str, Any], timestamp: int | None) -> None:
        event = self.pending.pop(block.get("tool_use_id"), None)
        if event is None:
            return
        event.source_lines.append(line)
        event.ended_at = timestamp
        result = record.get("toolUseResult")
        text = _text(block.get("content"))
        if text.startswith(DECLINED) or (isinstance(result, str) and DECLINED[0] in result):
            event.status = "declined"
        elif isinstance(result, dict) and result.get("interrupted"):
            event.status = "interrupted"
        elif block.get("is_error"):
            event.status = "failed"
        else:
            event.status = "completed"
        _complete(event, result if isinstance(result, dict) else None, text)

    # -- helpers ----------------------------------------------------------------

    def _add(self, line: int, record: dict[str, Any], kind: str, status: str | None, timestamp: int | None,
             payload: dict[str, Any]) -> Event:
        sidechain = bool(record.get("isSidechain"))
        event = Event(
            id=f"{self.session.id}:{len(self.events) + 1}", session_id=self.session.id,
            thread_id=self._sidechain_thread() if sidechain else self.thread_id,
            turn_id=self.turn.id if self.turn else None, seq=len(self.events) + 1, kind=kind, status=status,
            started_at=timestamp, ended_at=timestamp, payload=payload, source_lines=[line])
        self.events.append(event)
        return event

    def _sidechain_thread(self) -> str:
        return f"{self.thread_id}/sidechain"

    def _usage(self, message: dict[str, Any]) -> dict[str, Any]:
        usage = message.get("usage") or {}
        cached = (usage.get("cache_read_input_tokens") or 0)
        last = {
            "input": (usage.get("input_tokens") or 0) + cached + (usage.get("cache_creation_input_tokens") or 0),
            "cached_input": cached,
            "output": usage.get("output_tokens") or 0,
            "reasoning_output": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
        }
        last["total"] = last["input"] + last["output"]
        for key in ("input", "cached_input", "output", "total"):
            self.totals[key] = self.totals.get(key, 0) + last[key]
        return {"total": dict(self.totals), "last": last, "model": message.get("model")}

    def _finish(self) -> NormalizedSession:
        for event in self.pending.values():  # tool calls that never got a result
            event.status = "interrupted" if event.turn_id in self.interrupted else "incomplete"
        for turn in self.turns.values():
            if turn.id in self.interrupted:
                turn.status = "interrupted"
            elif self.stop_reason.get(turn.id) == "end_turn":
                turn.status = "completed"
            else:
                turn.status = "incomplete"
            in_turn = [e for e in self.events if e.turn_id == turn.id]
            turn.ended_at = max((e.ended_at or e.started_at or 0 for e in in_turn), default=turn.started_at)
            replies = [e for e in in_turn if e.kind == model.AGENT_MESSAGE]
            if replies and turn.status == "completed":
                replies[-1].payload["phase"] = "final"  # Claude Code doesn't label its final answer
        turns = sorted(self.turns.values(), key=lambda t: t.seq)
        self.session.outcome = turns[-1].status if turns else None
        root = self.thread_id or self.session.id
        threads = [Thread(root, self.session.id)]
        if any(e.thread_id == self._sidechain_thread() for e in self.events):
            threads.append(Thread(self._sidechain_thread(), self.session.id, root))
        return NormalizedSession(self.session, threads, turns, self.events)


def _tool_call(block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(kind, payload) for a tool_use block, before its result is known."""
    name = block.get("name")
    args = block.get("input") or {}
    base = {"source_type": name, "source_id": block.get("id")}
    if name == "Bash":
        return model.COMMAND, {**base, "command": args.get("command"), "description": args.get("description"),
                                     "exit_code": None, "output": None, "actions": []}
    if name in FILE_TOOLS:
        path = args.get("file_path") or args.get("notebook_path")
        return model.FILE_CHANGE, {**base, "tool": name, "paths": [path] if path else []}
    if name in READ_TOOLS:
        target = args.get("file_path") or args.get("path") or args.get("notebook_path")
        query = args.get("pattern")
        return model.TOOL_CALL, {**base, "tool": name, "arguments": args, "actions": [
            {"type": READ_TOOLS[name], "path": target, "command": f"{name} {query}" if query else None}]}
    if name in SUBAGENT_TOOLS:
        return model.SUBAGENT_CALL, {**base, "action": "spawn", "prompt": args.get("prompt"),
                                           "description": args.get("description"),
                                           "agent_type": args.get("subagent_type"), "receiver_thread_ids": []}
    return model.TOOL_CALL, {**base, "tool": name, "server": None, "arguments": args}


def _complete(event: Event, result: dict[str, Any] | None, text: str) -> None:
    """Fill in what the tool result tells us."""
    payload = event.payload
    if event.kind == model.COMMAND:
        match = EXIT_CODE.match(text)
        if event.status == "completed":
            payload["exit_code"] = 0
        elif event.status == "failed" and match:
            payload["exit_code"] = int(match[1])
        if result is not None:
            payload["output"] = "".join(p for p in (result.get("stdout"), result.get("stderr")) if p)
        else:  # failures arrive as text: "Exit code N\n<output>"
            payload["output"] = text.split("\n", 1)[1] if match and "\n" in text else text
    elif event.kind == model.FILE_CHANGE:
        if result is not None:
            payload["user_modified"] = result.get("userModified")
        if event.status == "completed" and result is not None:
            change = _file_change(event, result)
            event.file_changes = [change] if change else []
    elif event.kind == model.SUBAGENT_CALL:
        payload["result"] = text
    elif event.kind == model.TOOL_CALL and payload.get("tool") not in READ_TOOLS:
        payload["result"] = result if result is not None else text  # file contents from Read aren't kept


def _file_change(event: Event, result: dict[str, Any]) -> FileChange | None:
    path = result.get("filePath") or (event.payload.get("paths") or [None])[0]
    if not path:
        return None
    if result.get("type") == "create":
        return FileChange(path, model.ADD, result.get("content") or "")
    hunks = result.get("structuredPatch") or []
    if hunks:
        return FileChange(path, model.UPDATE, _unified(hunks))
    if result.get("originalFile") is not None and result.get("content") is not None:
        return FileChange(path, model.UPDATE, _diff_contents(result["originalFile"], result["content"]))
    return None  # e.g. NotebookEdit: no line-level change recorded


def _unified(hunks: list[dict[str, Any]]) -> str:
    """Claude Code's structured hunks as unified diff text.

    Empty ranges follow jsdiff's convention (the start is the line after the gap); unified
    diff numbers an empty range by the line before it, as jsdiff's formatPatch does.
    """
    out = []
    for hunk in hunks:
        old_start, old_lines = hunk.get("oldStart", 0), hunk.get("oldLines", 0)
        new_start, new_lines = hunk.get("newStart", 0), hunk.get("newLines", 0)
        if old_lines == 0:
            old_start -= 1
        if new_lines == 0:
            new_start -= 1
        out.append(f"@@ -{old_start},{old_lines} +{new_start},{new_lines} @@")
        out += [line for line in hunk.get("lines") or [] if not line.startswith("\\")]
    return "\n".join(out) + "\n"


def _diff_contents(before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++"))) + "\n"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _records(lines: Any) -> Any:
    for number, raw in enumerate(lines, start=1):
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield number, record


def _ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None
