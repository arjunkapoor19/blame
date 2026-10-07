"""Agent blame: which agent event wrote each line of a file, and what surrounded it.

Recorded file changes are replayed in order into a line-origin map. ADD sets
every line; UPDATE hunks are applied by their line numbers, so the original file
content is never needed. Lines no recorded change touched stay unattributed.

When the file exists on disk, the replayed lines are aligned with the disk
content, so lines edited or added outside recorded history are reported as such
instead of being blamed on whichever agent change last touched that position.

Known gap: edits made through shell commands (sed, `echo >>`) are not file
changes and are invisible here.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_history import commands, model
from agent_history.model import Event
from agent_history.store import Store

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
STORY_BEFORE = 12  # steps shown before the change; older ones are counted, not shown
STORY_AFTER = 6  # steps shown after the change while waiting for a test run


@dataclass
class Line:
    text: str | None  # None: a pre-existing line whose content was never recorded
    origin: str | None = None  # id of the event that wrote this line
    history: list[tuple[str, str]] = field(default_factory=list)  # (event id, text), oldest first
    born: int | None = None  # its line number in the file right after `origin` wrote it


@dataclass
class Story:
    """The turn that wrote a line, as an ordered list of steps."""
    prompt: str | None
    steps: list[Event]  # includes the origin event itself
    hidden_before: int  # earlier steps left out


@dataclass
class BlamedLine:
    number: int
    text: str | None
    origin: Event | None
    history: list[tuple[Event, str]]
    born: int | None = None  # its line number in the file right after its origin event

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
    for path, event_id, kind, diff, move_path in changes:
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
    return files


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
    candidates = {os.path.abspath(path), os.path.realpath(path)}
    warnings: list[str] = []
    rows = store.file_changes()
    files = replay([(r["path"], r["event_id"], r["kind"], r["diff"], r["move_path"]) for r in rows], warnings)
    target = next((c for c in candidates if c in files), None)
    if target is None:
        raise LookupError(f"no recorded agent changes to {path}")
    recorded = files[target]

    events: dict[str, Event] = {}

    def event(event_id: str | None) -> Event | None:
        if event_id is None:
            return None
        if event_id not in events:
            events[event_id] = store.event(event_id)
        return events[event_id]

    def blamed(number: int, text: str | None, line: Line | None) -> BlamedLine:
        origin = event(line.origin) if line else None
        if line is None or origin is None:
            return BlamedLine(number, text, None, [])
        return BlamedLine(number, text, origin, [(event(e), t) for e, t in line.history], line.born)

    disk = Path(target)
    if not disk.is_file():
        return FileBlame(target, [blamed(i, line.text, line) for i, line in enumerate(recorded, 1)],
                         on_disk=False, warnings=warnings)

    current = disk.read_text(encoding="utf-8", errors="replace").splitlines()
    # Unknown (None) recorded lines get unique placeholders so they never match disk content.
    recorded_texts = [line.text if line.text is not None else object() for line in recorded]
    matched: dict[int, Line] = {}
    for tag, i1, i2, j1, _ in difflib.SequenceMatcher(None, recorded_texts, current, autojunk=False).get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                matched[j1 + offset] = recorded[i1 + offset]
    lines = [blamed(j + 1, text, matched.get(j)) for j, text in enumerate(current)]
    return FileBlame(target, lines, on_disk=True, warnings=warnings)


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
    return Story(turn["input_text"] if turn else None, shown + [origin] + after, len(before) - len(shown))


def _worth_showing(event: Event) -> bool:
    if event.kind in (model.TOKEN_USAGE, model.USER_MESSAGE):
        return False
    return event.kind != model.REASONING or bool(event.payload.get("summary"))
