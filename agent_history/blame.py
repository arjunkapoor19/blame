"""Agent blame: who wrote each line of a file, and what surrounded it.

Two kinds of history are replayed, in time order:

  recorded  file changes agents report (edit tools). ADD sets every line; UPDATE hunks
            are applied by their line numbers, so the original content isn't needed.
  observed  snapshots `ah` takes around agents' tool calls (see hooks.py): the file's real
            content after each call. The replay is aligned to it, so lines that stayed keep
            their author and new ones belong to that call, whatever tool wrote them, even
            the shell. An observed tool call replaces the same call's recorded changes.

The first observation of a workspace is a baseline: aligning to it turns lines nothing
recorded into known pre-existing lines. Finally the result is aligned with the file on
disk, so later edits nobody observed are reported as such, never misattributed.
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from agent_history import commands, model
from agent_history.model import Event
from agent_history.store import Store
from agent_history.workspace import Blobs, unified_diff

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
STORY_BEFORE = 12  # steps shown before the change; older ones are counted, not shown
STORY_AFTER = 6  # steps shown after the change while waiting for a test run


OBSERVED = "obs:"  # origin prefix for lines written in an observed change: "obs:<observation id>"


@dataclass
class Line:
    text: str | None  # None: a pre-existing line whose content was never recorded
    origin: str | None = None  # id of the event, or OBSERVED + observation id, that wrote this line
    history: list[tuple[str, str]] = field(default_factory=list)  # (event id, text), oldest first
    born: int | None = None  # its line number in the file right after `origin` wrote it


@dataclass
class Story:
    """The turn that wrote a line, as an ordered list of steps."""
    prompt: str | None
    steps: list[Event]  # includes the origin event itself
    hidden_before: int  # earlier steps left out


@dataclass
class Author:
    """Who wrote a line: an agent (with its event, if its transcript is known), someone or
    something outside any agent, or nobody observed (the line predates observation)."""
    kind: str  # "agent" | "outside" | "baseline"
    agent: str | None = None
    event: Event | None = None
    observation: dict | None = None  # the observed tool call or outside change, if observed

    @property
    def concurrent(self) -> bool:
        return bool(self.observation and self.observation.get("concurrent"))


@dataclass
class BlamedLine:
    number: int
    text: str | None
    origin: Event | None  # the event that wrote the line, when known
    history: list[tuple[Author | None, str]]
    born: int | None = None  # its line number in the file right after it was written
    author: Author | None = None

    @property
    def rewrites(self) -> int:
        return max(len(self.history) - 1, 0)


@dataclass
class FileBlame:
    path: str
    lines: list[BlamedLine]
    on_disk: bool
    warnings: list[str]


def replay(changes: list[tuple[str, str, str, str | None, str | None]],
           warnings: list[str] | None = None) -> dict[str, list[Line]]:
    """Replay ordered (path, event_id, kind, diff, move_path) changes into per-path line maps."""
    files: dict[str, list[Line]] = {}
    for change in changes:
        _apply_recorded(files, *change, warnings=warnings)
    return files


def _apply_recorded(files: dict[str, list[Line]], path: str, event_id: str, kind: str, diff: str | None,
                    move_path: str | None, warnings: list[str] | None = None) -> None:
    if kind == model.ADD:
        files[path] = [Line(text, event_id, [(event_id, text)], number)
                       for number, text in enumerate((diff or "").splitlines(), start=1)]
    elif kind == model.DELETE:
        files.pop(path, None)
    else:
        lines = files.setdefault(path, [])
        apply_hunks(lines, diff or "", event_id, warnings)
        if kind == model.MOVE and move_path:
            files[move_path] = files.pop(path)


def align(lines: list[Line], texts: list[str], origin: str | None) -> list[Line]:
    """Make `lines` read `texts`: lines that stay keep their origin; the rest are new lines from
    `origin` (None: unattributed), carrying the history of the lines they replaced."""
    old = [line.text if line.text is not None else object() for line in lines]  # unknown never matches
    out: list[Line] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, texts, autojunk=False).get_opcodes():
        if tag == "equal":
            out += lines[i1:i2]
            continue
        replaced = lines[i1:i2] if tag == "replace" else []
        for offset, j in enumerate(range(j1, j2)):
            before = replaced[offset] if offset < len(replaced) else None
            history = before.history if before is not None and before.origin else []
            out.append(Line(texts[j], origin, history + [(origin, texts[j])] if origin else [], j + 1))
    return out


def apply_hunks(lines: list[Line], diff: str, event_id: str, warnings: list[str] | None = None) -> None:
    """Apply unified-diff hunks to `lines` in place, attributing added lines to `event_id`."""
    hunk: list[str] | None = None
    start = 0
    for raw in diff.splitlines() + ["@@ end"]:
        if raw.startswith("@@"):
            if hunk is not None:
                _apply_hunk(lines, start, hunk, event_id)
            match = HUNK_HEADER.match(raw)
            if match:
                new_start, new_count = int(match[3]), int(match[4]) if match[4] is not None else 1
                start = new_start - 1 if new_count > 0 else new_start
                hunk = []
            else:
                if raw != "@@ end" and warnings is not None:
                    warnings.append(f"skipped a hunk without line numbers in {event_id}")
                hunk = None
        elif hunk is not None:
            hunk.append(raw)


def _apply_hunk(lines: list[Line], index: int, body: list[str], event_id: str) -> None:
    def pad(length: int) -> None:
        while len(lines) < length:
            lines.append(Line(None))

    removed: list[Line] = []  # the run of '-' lines an added line may be replacing
    for raw in body:
        tag, text = (raw[0], raw[1:]) if raw else (" ", "")
        if tag == "\\":  # "\ No newline at end of file"
            continue
        if tag == "-":
            pad(index + 1)
            removed.append(lines.pop(index))
        elif tag == "+":
            pad(index)
            replaced = removed.pop(0) if removed else None
            history = (replaced.history if replaced else []) + [(event_id, text)]
            lines.insert(index, Line(text, event_id, history, index + 1))
            index += 1
        else:  # context
            removed = []
            pad(index + 1)
            if lines[index].text is None:
                lines[index].text = text
            index += 1


def blame(store: Store, path: str) -> FileBlame:
    candidates = sorted({os.path.abspath(path), os.path.realpath(path)})
    warnings: list[str] = []
    blobs = Blobs(store.objects_dir)
    observed = store.observed_changes(candidates)
    links = {r["observation_id"]: store.linked_event_id(r["agent_session_id"], r["tool_call_id"])
             for r in observed if r["actor"] == "agent"}
    superseded = {(links[r["observation_id"]], r["path"]) for r in observed if links.get(r["observation_id"])}
    baseline = next((b for b in map(store.baseline, candidates) if b is not None), None)

    # Everything that happened to files, in time order: (time, order, kind, row).
    timeline = [(r["started_at"] or 0, 1, "recorded", r) for r in store.file_changes()
                if (r["event_id"], r["path"]) not in superseded]
    timeline += [(r["ended_at"], 2, "observed", r) for r in observed]
    if baseline is not None:
        timeline.append((baseline["started_at"], 0, "baseline", baseline))
    files: dict[str, list[Line]] = {}
    for _, _, kind, row in sorted(timeline, key=lambda item: item[:2]):
        if kind == "recorded":
            _apply_recorded(files, row["path"], row["event_id"], row["kind"], row["diff"], row["move_path"], warnings)
        elif row["kind"] == model.DELETE:
            files.pop(row["path"], None)
        else:
            text = blobs.text(row["after_blob"])
            if text is None:
                warnings.append(f"{row['path']}: content not kept (binary or too large)")
                continue
            origin = f"{OBSERVED}{row['observation_id']}"
            files[row["path"]] = align(files.get(row["path"], []), text.splitlines(), origin)
    target = next((c for c in candidates if c in files), None)
    if target is None:
        raise LookupError(f"no recorded or observed agent changes to {path}")
    replayed = files[target]

    authors: dict[str, Author | None] = {}

    def author(origin: str | None) -> Author | None:
        if origin is None:
            return None
        if origin not in authors:
            authors[origin] = _resolve(store, origin, links)
        return authors[origin]

    def blamed(number: int, text: str | None, line: Line) -> BlamedLine:
        who = author(line.origin)
        if who is None:
            return BlamedLine(number, text, None, [])
        return BlamedLine(number, text, who.event, [(author(o), t) for o, t in line.history], line.born, who)

    disk = Path(target)
    if disk.is_file():  # lines nobody observed changing stay unattributed
        replayed = align(replayed, disk.read_text(encoding="utf-8", errors="replace").splitlines(), None)
    return FileBlame(target, [blamed(i, line.text, line) for i, line in enumerate(replayed, 1)],
                     on_disk=disk.is_file(), warnings=warnings)


def _resolve(store: Store, origin: str, links: dict) -> Author | None:
    if not origin.startswith(OBSERVED):
        event = store.event(origin)
        if event is None:
            return None
        session = store.find_session(event.session_id)
        return Author("agent", session["agent"] if session else None, event)
    observation = store.observation(int(origin[len(OBSERVED):]))
    if observation is None:
        return None
    row = dict(observation)
    event_id = links.get(row["id"]) or store.linked_event_id(row["agent_session_id"], row["tool_call_id"])
    return Author(row["actor"], row["agent"], store.event(event_id) if event_id else None, row)


def observed_file_changes(store: Store, observation_id: int) -> list[model.FileChange]:
    """An observation's changes as file changes with diffs, for display."""
    blobs = Blobs(store.objects_dir)
    out = []
    for row in store.changes_of(observation_id):
        before, after = blobs.text(row["before_blob"]), blobs.text(row["after_blob"])
        if row["kind"] == model.ADD:
            diff = after
        elif row["kind"] == model.UPDATE and before is not None and after is not None:
            diff = unified_diff(before, after)
        else:
            diff = None
        out.append(model.FileChange(row["path"], row["kind"], diff, observed=True))
    return out


def story(store: Store, origin: Event) -> Story:
    """The origin's turn: steps leading up to the change, the change, then up to the next test run."""
    turn = store.turn(origin.turn_id) if origin.turn_id else None
    steps = [e for e in store.events(turn_id=origin.turn_id) if _worth_showing(e)] if turn else [origin]
    before = [e for e in steps if e.seq < origin.seq]
    after: list[Event] = []
    for event in (e for e in steps if e.seq > origin.seq):
        if len(after) == STORY_AFTER:
            break
        after.append(event)
        if event.kind == model.COMMAND and commands.verdict(event.payload, event.status):
            break
    final = next((e for e in reversed(steps) if e.kind == model.AGENT_MESSAGE
                  and e.payload.get("phase") == "final"), None)
    if final is not None and final.seq > origin.seq and final not in after:
        after.append(final)
    shown = before[-STORY_BEFORE:]
    for event in shown + [origin] + after:  # what ah saw each step change replaces what the agent reported
        observation = store.observation_for(event)
        if observation is not None:
            event.file_changes = observed_file_changes(store, observation["id"])
    return Story(turn["input_text"] if turn else None, shown + [origin] + after, len(before) - len(shown))


def _worth_showing(event: Event) -> bool:
    if event.kind in (model.TOKEN_USAGE, model.USER_MESSAGE):
        return False
    return event.kind != model.REASONING or bool(event.payload.get("summary"))


def delegation(store: Store, turn: sqlite3.Row | None) -> tuple[sqlite3.Row | None, list[tuple[str, str | None]]]:
    """For a sub-agent's turn: the person's turn it was delegated from, and each hand-off on the way
    (sub-agent thread, prompt it was given), outermost first. Any other turn is its own top."""
    chain: list[tuple[str, str | None]] = []
    while turn is not None:
        spawn = store.spawner(turn["thread_id"])
        if spawn is None:
            break
        chain.insert(0, (turn["thread_id"], turn["input_text"]))
        turn = store.turn(spawn.turn_id) if spawn.turn_id else None
    return turn, chain


def added_line_at(lines: list[str], number: int) -> int | None:
    """Index of the added diff line that lands at line `number` of the new file, if any."""
    new_line = 1
    for index, text in enumerate(lines):
        header = HUNK_HEADER.match(text)
        if header:
            new_line = int(header[3]) if int(header[4] or 1) else int(header[3]) + 1
        elif text.startswith("+"):
            if new_line == number:
                return index
            new_line += 1
        elif not text.startswith(("-", "\\")):
            new_line += 1
    return None
