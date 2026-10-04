"""`ah`: the Agent History command line."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent_history import model
from agent_history.adapters import codex
from agent_history.blame import BlamedLine, FileBlame, blame, story
from agent_history.commands import is_test, read_only_label, summarize
from agent_history.model import Event
from agent_history.store import Store, default_db_path

COMMAND_MARKS = {"completed": "✓", "failed": "✗", "declined": "⊘", "interrupted": "…", "incomplete": "…"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ah", description="Git-like history for AI coding agents.")
    parser.add_argument("--db", default=str(default_db_path()),
                        help="SQLite database (default: $AGENT_HISTORY_DB or ~/.agent-history/history.db)")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Normalize recorded sessions into the database.")
    ingest.add_argument("sources", nargs="+", help="Capture directories written by recorder/codex_recorder.py")
    log = commands.add_parser("log", help="List sessions, or show one session's timeline.")
    log.add_argument("session", nargs="?", help="Session id (or a unique part of it)")
    blame_cmd = commands.add_parser("blame", help="Show which agent event wrote each line of a file.")
    blame_cmd.add_argument("target", help="PATH or PATH:LINE")
    args = parser.parse_args(argv)

    store = Store(args.db)
    try:
        if args.command == "ingest":
            return cmd_ingest(store, args.sources)
        if args.command == "log":
            return cmd_log(store, args.session)
        return cmd_blame(store, args.target)
    except LookupError as error:
        print(f"ah: {error.args[0]}", file=sys.stderr)
        return 1
    finally:
        store.close()


def cmd_ingest(store: Store, sources: list[str]) -> int:
    status = 0
    for source in sources:
        if not (Path(source) / "events.jsonl").is_file():
            print(f"ah: {source}: not a capture directory (no events.jsonl)", file=sys.stderr)
            status = 1
            continue
        normalized = codex.normalize(source)
        store.ingest(normalized)
        changes = sum(len(e.file_changes) for e in normalized.events)
        print(f"ingested {normalized.session.id}: {len(normalized.turns)} turns, "
              f"{len(normalized.events)} events, {changes} file changes")
    return status


def cmd_log(store: Store, query: str | None) -> int:
    if query is None:
        for s in store.sessions():
            version = f" {s['agent_version']}" if s["agent_version"] else ""
            print(f"{s['id']}  {s['agent']}{version}  {_time(s['started_at'], date=True)}  "
                  f"{s['turn_count']} turns  {s['outcome'] or '?'}  {_quote(s['first_input'], 60)}")
        return 0
    session = store.find_session(query)
    if session is None:
        raise LookupError(f"no session matching '{query}'")
    print(f"session {session['id']}  {session['agent']} {session['agent_version'] or ''}  "
          f"{session['outcome'] or '?'}  {session['cwd'] or ''}")
    events = store.events(session_id=session["id"])
    for turn in store.turns(session["id"]):
        duration = _duration(turn["started_at"], turn["ended_at"])
        print(f"\nturn {turn['seq']}  {turn['status']}{duration}")
        for event in events:
            if event.turn_id == turn["id"]:
                line = describe(event, session["cwd"])
                if line:
                    print(f"  {_time(event.started_at)}  {line}")
    return 0


def cmd_blame(store: Store, target: str) -> int:
    path, _, number = target.rpartition(":")
    if not (path and number.isdigit()):
        path, number = target, ""
    result = blame(store, path)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not number:
        return _blame_file(store, result)
    index = int(number)
    if not 1 <= index <= len(result.lines):
        raise LookupError(f"{result.path} has {len(result.lines)} lines")
    return _blame_line(store, result.path, result.lines[index - 1])


def _blame_file(store: Store, result: FileBlame) -> int:
    if not result.on_disk:
        print("(file not on disk; showing content reconstructed from recorded changes)", file=sys.stderr)
    width = len(str(len(result.lines)))
    for line in result.lines:
        who = _who(store, line.origin) if line.origin else ""
        print(f"{who:<20}  {line.number:>{width}}  {line.text if line.text is not None else '…'}")
    return 0


def _blame_line(store: Store, path: str, line: BlamedLine) -> int:
    print(f"{path}:{line.number}")
    print(f"    {line.text if line.text is not None else '… (content not recorded)'}\n")
    if line.origin is None:
        print("Not written by a recorded agent: the line is pre-existing or was edited outside recorded history.")
        return 0

    origin = line.origin
    session = store.find_session(origin.session_id)
    cwd = session["cwd"] if session else None
    turn = store.turn(origin.turn_id) if origin.turn_id else None
    print(f"written by {session['agent'] if session else 'an agent'} · session {origin.session_id} · "
          f"turn {turn['seq'] if turn else '?'} · {_time(origin.started_at, date=True)} UTC\n")

    told = story(store, origin)
    if told.prompt:
        print(f"You asked: {_quote(told.prompt, 300)}\n")
    if told.hidden_before:
        print(f"    … {told.hidden_before} earlier steps (ah log {origin.session_id})")
    for event in told.steps:
        text = describe(event, cwd)
        if event.id == origin.id:
            print(f"▶ {_time(event.started_at)}  {text}   ← wrote this line")
        elif text:
            print(f"  {_time(event.started_at)}  {text}")

    if line.rewrites:
        print(f"\nLine history (oldest first; rewritten {line.rewrites} time{'s' if line.rewrites > 1 else ''})")
        for event, text in line.history:
            print(f"  {_who(store, event) if event else '?':<22}  {text}")
    print("\nSteps are shown in order; order is not proof of cause.")
    return 0


def _who(store: Store, event: Event) -> str:
    """Short `session turn time` label, e.g. `39de3d t1   13:22:33`."""
    turn = store.turn(event.turn_id) if event.turn_id else None
    return f"{event.session_id[-6:]:<6} t{turn['seq'] if turn else '?':<3} {_time(event.started_at)}"


def describe(event: Event, cwd: str | None = None) -> str | None:
    """One-line summary of an event, or None if it isn't worth a line in a timeline."""
    p = event.payload
    if event.kind == model.USER_MESSAGE:
        return f"user     {_quote(p.get('text'), 100)}"
    if event.kind == model.AGENT_MESSAGE:
        label = "answer" if p.get("phase") == "final" else "agent"
        return f"{label:<8} {_first_line(p.get('text'), 100)}"
    if event.kind == model.COMMAND:
        reads = read_only_label(p) if event.status == "completed" else None
        if reads:
            return f"read     {reads}"
        label = "test" if is_test(p.get("command")) else "command"
        mark = COMMAND_MARKS.get(event.status or "", "?")
        summary = summarize(p, event.status)
        if summary is None and p.get("exit_code") not in (0, None):
            summary = f"exit {p['exit_code']}"
        return f"{label:<8} {mark} {_first_line(p.get('command'), 60)}{'  → ' + summary if summary else ''}"
    if event.kind == model.FILE_CHANGE:
        signs = {model.ADD: "+", model.DELETE: "-", model.UPDATE: "~", model.MOVE: "→"}
        files = ", ".join(f"{signs.get(c.kind, '~')}{_relative(c.path, cwd)}" for c in event.file_changes)
        status = "" if event.status in (None, "completed") else f"  ({event.status})"
        return f"edit     {files}{status}"
    if event.kind == model.APPROVAL:
        return f"approval {event.status or 'unanswered'}: {_first_line(p.get('command') or p.get('subject'), 80)}"
    if event.kind == model.TOOL_CALL:
        name = "/".join(str(x) for x in (p.get("server"), p.get("tool")) if x)
        return f"tool     {name} ({event.status})"
    if event.kind == model.SUBAGENT_CALL:
        receivers = ", ".join(t[-8:] for t in p.get("receiver_thread_ids") or [])
        return f"subagent {p.get('action')} → {receivers or '?'}  {_quote(p.get('prompt'), 60)}"
    if event.kind == model.ERROR:
        return f"error    {_first_line(p.get('message'), 100)}"
    if event.kind == model.REASONING and p.get("summary"):
        return f"thinking {_first_line(' '.join(map(str, p['summary'])), 100)}"
    return None


def _relative(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return os.path.relpath(path, cwd)
    return path


def _first_line(text: str | None, limit: int) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _quote(text: str | None, limit: int) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    return '"' + (flat if len(flat) <= limit else flat[: limit - 1] + "…") + '"'


def _time(ms: int | None, date: bool = False) -> str:
    if ms is None:
        return "--:--:--"
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S" if date else "%H:%M:%S")


def _duration(start: int | None, end: int | None) -> str:
    return f"  {(end - start) / 1000:.1f}s" if start is not None and end is not None else ""


if __name__ == "__main__":
    raise SystemExit(main())
