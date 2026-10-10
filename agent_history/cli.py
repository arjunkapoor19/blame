"""`ah`: the Agent History command line."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from agent_history import hooks, model, sources
from agent_history import setup as agent_setup
from agent_history.blame import (Author, BlamedLine, FileBlame, added_line_at, blame, delegation,
                                 observed_file_changes, story)
from agent_history.present import author_name, outline, relative
from agent_history.model import Event
from agent_history.store import DEFAULT_DB, Store, default_db_path

DIFF_PREVIEW = 12  # diff lines shown per file for edits other than the one that wrote the line
ORIGIN_WINDOW = 8  # diff lines shown either side of the blamed line when its edit is large
DETAIL = " " * 21  # indents an event's details under its text in a story
WIDTH = 100
DIFF_COLORS = {"+": "32", "-": "31"}
AUTHOR_WIDTH = 8  # the longest agent name so far
WHO_WIDTH = 29  # `session turn when`: 6 + 2 + 4 + 1 + 16
AGENT_COLORS = ("35", "36", "34", "95", "96")  # per agent name, stable across runs

COMMAND_MARKS = {"completed": "✓", "failed": "✗", "declined": "⊘", "interrupted": "…", "incomplete": "…"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ah", description="Git-like history for AI coding agents.")
    parser.add_argument("--db", default=str(default_db_path()),
                        help="SQLite database (default: $AGENT_HISTORY_DB or ~/.agent-history/history.db)")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Load agent sessions into the database (default: sync all).")
    ingest.add_argument("paths", nargs="*",
                        help="Session records to load, e.g. a recorder capture directory or an agent's log "
                             "file. Without paths, sync every agent's logs on this machine.")
    log = commands.add_parser("log", help="List sessions, or show one session's timeline.")
    log.add_argument("session", nargs="?", help="Session id (or a unique part of it)")
    blame_cmd = commands.add_parser("blame", help="Show which agent event wrote each line of a file.")
    blame_cmd.add_argument("target", help="PATH or PATH:LINE")
    setup_cmd = commands.add_parser("setup", help="Install hooks so ah sees every change agents make, "
                                                  "including through the shell.")
    setup_cmd.add_argument("agents", nargs="*", help=f"Agents to set up: {', '.join(agent_setup.AGENTS)} "
                                                      "(default: all)")
    setup_cmd.add_argument("--remove", action="store_true", help="Remove ah's hooks instead.")
    hook_cmd = commands.add_parser("hook", help="(Run by agents.) Observe the workspace around a tool call.")
    hook_cmd.add_argument("agent")
    hook_cmd.add_argument("event", choices=hooks.EVENTS)
    args = parser.parse_args(argv)

    if args.command == "hook":
        return hooks.run(args.agent, args.event, sys.stdin.read(), args.db)
    if args.command == "setup":
        try:
            return cmd_setup(args.agents or list(agent_setup.AGENTS), args.remove, args.db)
        except (LookupError, ValueError) as error:
            print(f"ah: {error.args[0]}", file=sys.stderr)
            return 1
    store = Store(args.db)
    try:
        if args.command == "ingest":
            return cmd_ingest(store, args.paths)
        _report_sync(sources.sync(store))
        if args.command == "log":
            return cmd_log(store, args.session)
        return cmd_blame(store, args.target)
    except LookupError as error:
        print(f"ah: {error.args[0]}", file=sys.stderr)
        return 1
    finally:
        store.close()


def cmd_setup(agents: list[str], remove: bool, db: str) -> int:
    unknown = [name for name in agents if name not in agent_setup.AGENTS]
    if unknown:
        raise LookupError(f"unknown agent {', '.join(unknown)} (choose from {', '.join(agent_setup.AGENTS)})")
    ah = agent_setup.ah_executable()
    custom_db = db if Path(db) != DEFAULT_DB else None
    for name in agents:
        agent = agent_setup.AGENTS[name]
        if remove:
            removed = agent_setup.remove(agent)
            print(f"{name}: {'removed ah hooks from' if removed else 'no ah hooks in'} {agent.settings()}")
            continue
        print(f"{name}: added hooks to {agent.settings()}")
        for line in agent_setup.install(agent, ah, custom_db):
            print(f"  {line}")
    if not remove:
        print(f"\nHooks run {ah}")
        if ".venv" in Path(ah).parts:
            print("  That's inside a project virtualenv; for a stable, fast `ah`, run `uv tool install --editable .`"
                  " and then `ah setup` again.")
        for name in agents:
            if agent_setup.AGENTS[name].note:
                print(agent_setup.AGENTS[name].note)
        print("Undo with `ah setup --remove`. A backup of each settings file was kept as *.agent-history.bak.")
    return 0


def cmd_ingest(store: Store, paths: list[str]) -> int:
    if not paths:
        results = sources.sync(store)
        if not results:
            print("everything is up to date")
    else:
        results = []
        for path in paths:
            try:
                results.append(sources.ingest(store, Path(path)))
            except LookupError as error:
                print(f"ah: {error.args[0]}", file=sys.stderr)
                return 1
    for result in results:
        detail = f": {result.reason}" if result.reason else ""
        print(f"{result.status:<8} {result.session_id or '-'}  ({result.source}) {result.path}{detail}")
    return 1 if any(r.status == sources.FAILED for r in results) else 0


def _report_sync(results: list[sources.Result]) -> None:
    """One quiet line on stderr when the automatic sync picked something up."""
    ingested = Counter(r.source for r in results if r.status == sources.INGESTED)
    failed = [r for r in results if r.status == sources.FAILED]
    if ingested:
        by_source = ", ".join(f"{name}: {count}" for name, count in sorted(ingested.items()))
        print(f"synced {sum(ingested.values())} new or updated sessions ({by_source})", file=sys.stderr)
    for result in failed:
        print(f"warning: could not read {result.path}: {result.reason}", file=sys.stderr)


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
        top, chain = delegation(store, turn)
        via = f"  sub-agent {turn['thread_id'][-8:]}, from turn {top['seq'] if top else '?'}" if chain else ""
        print(f"\nturn {turn['seq']}  {turn['status']}{duration}{via}")
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
    print(_color("2", f"{'author':<{AUTHOR_WIDTH}} {'session turn when':<{WHO_WIDTH}}  {'#':>{width}}  code"))
    for line in result.lines:
        name = _author_name(line.author)
        who = _who(store, line.author) if line.author and line.author.kind != "baseline" else ""
        flag = "≈" if line.author and line.author.concurrent else " "
        text = line.text if line.text is not None else "…"
        print(f"{_color(_author_color(name), f'{name:<{AUTHOR_WIDTH}}')} {who:<{WHO_WIDTH}}{flag} "
              f"{line.number:>{width}}  {text}")
    counts = Counter(_author_name(line.author) or "untracked" for line in result.lines)
    labels = {"·": "pre-existing", "you": "outside any agent"}
    print("\n" + " · ".join(f"{count} {labels.get(name, name)}" for name, count in counts.most_common()))
    if any(line.author and line.author.concurrent for line in result.lines):
        print("≈ written while another tool call was running in the same workspace: it may have come from either")
    return 0


def _blame_line(store: Store, path: str, line: BlamedLine) -> int:
    print(f"{path}:{line.number}")
    print(f"    {line.text if line.text is not None else '… (content not recorded)'}\n")
    author = line.author
    if author is None:
        print("Not attributed: the line predates what ah has recorded or observed, or changed while nothing "
              "was observing.\nRun `ah setup` so ah observes every change from now on.")
        return 0
    if author.kind == "baseline":
        print(f"Already here when ah started observing this workspace ({_time(author.observation['started_at'], True)})."
              "\nNo agent has changed it since.")
        return 0
    if author.concurrent:
        print("≈ Another tool call was running in this workspace at the same time; the line may be from either.\n")
    if author.kind == "outside":
        obs = author.observation
        print(f"Edited outside any agent (by you or another program) between {_time(obs['started_at'], True)} "
              f"and {_time(obs['ended_at'], True)}.\n")
        _print_observed(store, obs["id"], path, line.born)
        return 0
    if line.origin is None:  # an observed tool call whose transcript isn't in the database yet
        obs = author.observation
        print(f"written by {author.agent} · session {obs['agent_session_id']} · {obs['tool_name']} · "
              f"{_time(obs['ended_at'], True)}\n(this session's transcript hasn't been synced yet)\n")
        if obs["command"]:
            print(f"  {_first_line(obs['command'], WIDTH)}")
        _print_observed(store, obs["id"], path, line.born)
        return 0

    origin = line.origin
    session = store.find_session(origin.session_id)
    cwd = session["cwd"] if session else None
    top, chain = delegation(store, store.turn(origin.turn_id) if origin.turn_id else None)
    via = "".join(f" › sub-agent {thread[-8:]}" for thread, _ in chain)
    seen = " · observed by ah" if author.observation else ""
    print(f"written by {author.agent or 'an agent'} · session {origin.session_id} · "
          f"turn {top['seq'] if top else '?'}{via} · {_time(origin.started_at, date=True)}{seen}\n")

    told = story(store, origin)
    if chain:
        if top is not None and top["input_text"]:
            print(f"You asked: {_quote(top['input_text'], 300)}")
        for thread, prompt in chain:
            print(f"  → handed to sub-agent {thread[-8:]}: {_quote(prompt or '', 300)}")
        print()
    elif told.prompt:
        print(f"You asked: {_quote(told.prompt, 300)}\n")
    if told.hidden_before:
        print(f"    … {told.hidden_before} earlier steps (ah log {origin.session_id})")
    for event in told.steps:
        lines = _story_lines(event, cwd, path, line.born if event.id == origin.id else None)
        if not lines:
            continue
        if event.id == origin.id:
            print(f"▶ {_time(event.started_at)}  {lines[0]}   ← wrote this line")
        else:
            print(f"  {_time(event.started_at)}  {lines[0]}")
        for detail in lines[1:]:
            print(DETAIL + detail)

    if line.rewrites:
        print(f"\nLine history (oldest first; rewritten {line.rewrites} time{'s' if line.rewrites > 1 else ''})")
        for who, text in line.history:
            name = _author_name(who) or "?"
            print(f"  {_color(_author_color(name), f'{name:<{AUTHOR_WIDTH}}')} "
                  f"{_who(store, who) if who else '':<{WHO_WIDTH}}  {text}")
    print("\nSteps are shown in order; order is not proof of cause.")
    return 0


def _print_observed(store: Store, observation_id: int, path: str, born: int | None) -> None:
    for change in observed_file_changes(store, observation_id):
        if change.path == path:
            for detail in _diff_lines(change, None, born):
                print("  " + detail)


def _story_lines(event: Event, cwd: str | None, blamed_path: str, blamed_at: int | None) -> list[str]:
    """An event in a blame story: its summary line, then details (full messages, diffs).

    `blamed_at` is set for the event that wrote the blamed line (the line's number right after
    that edit): its diff for the blamed file is shown around that line, with the line marked.
    Other diffs are previews.
    """
    if event.kind == model.AGENT_MESSAGE:
        label = "answer" if event.payload.get("phase") == "final" else "agent"
        wrapped = _wrap(event.payload.get("text") or "", WIDTH - len(DETAIL))
        return [f"{label:<8} {wrapped[0] if wrapped else ''}"] + wrapped[1:]
    summary = describe(event, cwd)
    if summary is None or not event.file_changes:
        return [summary] if summary else []
    details: list[str] = []
    for change in event.file_changes:
        if len(event.file_changes) > 1 or event.kind != model.FILE_CHANGE:  # e.g. what a command changed
            details.append(("changed " if event.kind != model.FILE_CHANGE else "") + relative(change.path, cwd) + ":")
        origin = blamed_at is not None and change.path == blamed_path
        details += _diff_lines(change, None if origin else DIFF_PREVIEW, blamed_at if origin else None)
    return [summary] + details


def _diff_lines(change: model.FileChange, limit: int | None, mark: int | None) -> list[str]:
    """A file change as diff lines, at most `limit` of them; or, when `mark` (a line number in the
    file after the change) is given, the lines around the added line at that position, marked."""
    if change.kind == model.DELETE:
        return ["│ " + _color("31", "(file deleted)")]
    raw = change.diff or ""
    lines = ["+" + text for text in raw.splitlines()] if change.kind == model.ADD else raw.splitlines()
    marked = added_line_at(lines, mark) if mark is not None else None
    if marked is None:
        start, end = 0, len(lines) if limit is None else min(limit, len(lines))
    elif len(lines) <= 2 * ORIGIN_WINDOW + 1:
        start, end = 0, len(lines)
    else:
        start, end = max(0, marked - ORIGIN_WINDOW), min(len(lines), marked + ORIGIN_WINDOW + 1)
    out = ["│ " + _color("2", f"… {start} lines above")] if start else []
    for index in range(start, end):
        text = lines[index]
        rendered = "│ " + _color("2" if text.startswith("@@") else DIFF_COLORS.get(text[:1], ""), text)
        out.append(rendered + ("   ← this line" if index == marked else ""))
    if end < len(lines):
        out.append("│ " + _color("2", f"… {len(lines) - end} more lines"))
    return out


def _color(code: str, text: str) -> str:
    """ANSI colour, only when writing to a terminal and NO_COLOR isn't set."""
    if not code or not sys.stdout.isatty() or "NO_COLOR" in os.environ:
        return text
    return f"\033[{code}m{text}\033[0m"


def _wrap(text: str, width: int) -> list[str]:
    """Wrap each line of `text` separately, so paragraphs and lists keep their shape."""
    lines: list[str] = []
    for paragraph in text.strip().splitlines():
        lines += textwrap.wrap(paragraph, width) or [""]
    return lines


def _who(store: Store, author: Author) -> str:
    """Short `session turn when` label, e.g. `39de3d  t1   Oct 07 '26 13:22`."""
    event = author.event
    if event is not None:
        turn, _ = delegation(store, store.turn(event.turn_id) if event.turn_id else None)  # the turn you asked in
        return f"{event.session_id[-6:]:<6}  t{turn['seq'] if turn else '?':<3} {_when(event.started_at)}"
    obs = author.observation or {}
    return f"{(obs.get('agent_session_id') or '')[-6:]:<6}  {'':<4} {_when(obs.get('ended_at'))}"


def _author_name(author: Author | None) -> str:
    return author_name(author)[:AUTHOR_WIDTH]


def _author_color(name: str) -> str:
    if name in ("", "·"):
        return "2"
    if name == "you":
        return "33"
    return AGENT_COLORS[sum(map(ord, name)) % len(AGENT_COLORS)]


def describe(event: Event, cwd: str | None = None) -> str | None:
    """One-line summary of an event, or None if it isn't worth a line in a timeline."""
    step = outline(event, cwd)
    if step is None:
        return None
    label, title = step.label, step.title
    if label == "user":
        text = _quote(title, 100)
    elif label in ("test", "command"):
        mark = COMMAND_MARKS.get(step.status or "", "?")
        text = f"{mark} {_first_line(title, 60)}{'  → ' + step.summary if step.summary else ''}"
    elif label == "edit":
        text = title + ("" if step.status in (None, "completed") else f"  ({step.status})")
    elif label == "approval":
        text = f"{step.status or 'unanswered'}: {_first_line(title, 80)}"
    elif label == "tool":
        text = f"{title} ({step.status})"
    elif label == "subagent":
        text = f"{title}  {_quote(step.summary, 60)}"
    elif label == "read":
        text = title
    else:  # agent, answer, error, thinking
        text = _first_line(title, 100)
    return f"{label:<8} {text}"


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
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()  # the viewer's local time
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z" if date else "%H:%M:%S")


def _when(ms: int | None) -> str:
    """Date and minute for blame columns, always the same width: `Oct 07 '26 15:11`."""
    if ms is None:
        return "--- -- --- --:--"
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    return moment.strftime("%b %d '%y %H:%M")


def _duration(start: int | None, end: int | None) -> str:
    return f"  {(end - start) / 1000:.1f}s" if start is not None and end is not None else ""


if __name__ == "__main__":
    raise SystemExit(main())
