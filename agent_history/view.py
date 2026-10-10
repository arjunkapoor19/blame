"""`ah view`: the history in a browser.

A small local server: the viewer's page (built from `web/` into `static/`) and a read-only
JSON API over the same functions the terminal uses, so both always tell the same story.
It listens on 127.0.0.1 only and answers only requests addressed to it by that name
(`Host` checked, which stops DNS rebinding). The page opens at once; agent logs are
synced in the background every few seconds, and the page follows any change to the
database (a new session, or a hook observing an agent at work) as it happens.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from agent_history import model, sources
from agent_history.blame import (HUNK_HEADER, Author, added_line_at, blame, delegation,
                                 observed_file_changes, story)
from agent_history.model import Event, FileChange
from agent_history.present import author_name, outline, relative
from agent_history.store import Store

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 4545
SYNC_EVERY = 5.0  # seconds between background syncs; one that finds nothing new takes a few ms
DIFF_LIMIT = 2000  # diff lines sent per file change; a window around the marked line beyond that
OUTPUT_LIMIT = 20000  # characters of command output sent per step
IMMUTABLE = "public, max-age=31536000, immutable"  # built assets have content hashes in their names


# -- the API: plain functions from the store to JSON-ready dicts ------------------------------------

def files(store: Store) -> list[dict[str, Any]]:
    out = []
    for row in store.known_paths():
        root = row["root"] if row["root"] and row["path"].startswith(row["root"].rstrip("/") + "/") else None
        out.append({"path": row["path"], "root": root or os.path.dirname(row["path"]),
                    "changes": row["changes"], "agents": sorted(set(filter(None, (row["agents"] or "").split(",")))),
                    "last_changed": row["last_changed"]})
    return out


def blame_view(store: Store, path: str) -> dict[str, Any]:
    result = blame(store, path)
    authors: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    lines = []
    for line in result.lines:
        key = _author_key(line.author)
        if key is not None and key not in index:
            index[key] = len(authors)
            authors.append(_author(store, line.author, key))
        lines.append({"n": line.number, "text": line.text, "a": index.get(key) if key else None,
                      "rewrites": line.rewrites})
    totals = Counter(author_name(line.author) or "untracked" for line in result.lines)
    return {"path": result.path, "on_disk": result.on_disk, "warnings": result.warnings,
            "authors": authors, "lines": lines, "totals": dict(totals.most_common())}


def story_view(store: Store, path: str, number: int) -> dict[str, Any]:
    result = blame(store, path)
    if not 1 <= number <= len(result.lines):
        raise LookupError(f"{result.path} has {len(result.lines)} lines")
    line = result.lines[number - 1]
    base: dict[str, Any] = {"path": result.path, "line": number, "text": line.text}
    author = line.author
    if author is None:
        return {**base, "kind": "none"}
    base["author"] = _author(store, author, _author_key(author))
    if author.kind == "baseline":
        return {**base, "kind": "baseline"}
    obs = author.observation
    if author.kind == "outside":
        return {**base, "kind": "outside", "from": obs["started_at"], "to": obs["ended_at"],
                "changes": _observed(store, obs["id"], result.path, line.born)}
    if line.origin is None:  # an observed tool call whose transcript isn't in the database yet
        return {**base, "kind": "unsynced", "tool": obs["tool_name"], "command": obs["command"],
                "changes": _observed(store, obs["id"], result.path, line.born)}

    origin = line.origin
    session = store.find_session(origin.session_id)
    cwd = session["cwd"] if session else None
    top, chain = delegation(store, store.turn(origin.turn_id) if origin.turn_id else None)
    told = story(store, origin)
    steps = []
    for event in told.steps:
        step = _step(event, cwd, result.path, line.born if event.id == origin.id else None)
        if step is not None:
            steps.append(step)
    return {**base, "kind": "agent", "cwd": cwd, "agent_version": session["agent_version"] if session else None,
            "prompt": (top["input_text"] if chain and top else told.prompt),
            "handoffs": [{"thread": thread, "prompt": prompt} for thread, prompt in chain],
            "observed": author.observation is not None, "hidden_before": told.hidden_before, "steps": steps,
            "history": [{"author": _author(store, who, _author_key(who)) if who else None, "text": text}
                        for who, text in line.history] if line.rewrites else []}


def sessions_view(store: Store) -> list[dict[str, Any]]:
    return [{"id": s["id"], "agent": s["agent"], "agent_version": s["agent_version"], "cwd": s["cwd"],
             "started_at": s["started_at"], "ended_at": s["ended_at"], "outcome": s["outcome"],
             "turns": s["turn_count"], "first_input": s["first_input"]} for s in reversed(store.sessions())]


def session_view(store: Store, query: str) -> dict[str, Any]:
    session = store.find_session(query)
    if session is None:
        raise LookupError(f"no session matching '{query}'")
    events = store.events(session_id=session["id"])
    turns = []
    for turn in store.turns(session["id"]):
        top, chain = delegation(store, turn)
        steps = [step for step in (_step(e, session["cwd"], None, None, links=True)
                                   for e in events if e.turn_id == turn["id"]) if step is not None]
        turns.append({"id": turn["id"], "seq": turn["seq"], "status": turn["status"], "thread": turn["thread_id"],
                      "started_at": turn["started_at"], "ended_at": turn["ended_at"], "input": turn["input_text"],
                      "delegated_from": top["id"] if chain and top else None, "steps": steps})
    return {"id": session["id"], "agent": session["agent"], "agent_version": session["agent_version"],
            "cwd": session["cwd"], "started_at": session["started_at"], "ended_at": session["ended_at"],
            "outcome": session["outcome"], "turns": turns}


def _author_key(author: Author | None) -> str | None:
    if author is None:
        return None
    if author.event is not None:
        return author.event.id
    return f"obs:{author.observation['id']}" if author.observation else author.kind


def _author(store: Store, author: Author, key: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "kind": author.kind, "agent": author.agent, "name": author_name(author),
                           "concurrent": author.concurrent, "session": None, "turn": None, "turn_id": None,
                           "at": None}
    event, obs = author.event, author.observation or {}
    if event is not None:
        top, _ = delegation(store, store.turn(event.turn_id) if event.turn_id else None)  # the turn you asked in
        out.update(session=event.session_id, turn=top["seq"] if top else None,
                   turn_id=top["id"] if top else event.turn_id, at=event.started_at)
    else:
        out.update(session=obs.get("agent_session_id"), at=obs.get("ended_at") or obs.get("started_at"))
    return out


def _step(event: Event, cwd: str | None, blamed_path: str | None, blamed_at: int | None,
          links: bool = False) -> dict[str, Any] | None:
    step = outline(event, cwd)
    if step is None:
        return None
    out: dict[str, Any] = {"id": event.id, "at": event.started_at, "ended_at": event.ended_at,
                           "label": step.label, "title": step.title, "status": step.status,
                           "summary": step.summary, "origin": blamed_at is not None}
    if event.kind == model.COMMAND and not links:
        output = event.payload.get("output") or ""
        out.update(exit_code=event.payload.get("exit_code"), output=output[-OUTPUT_LIMIT:],
                   output_cut=max(len(output) - OUTPUT_LIMIT, 0))
    if event.file_changes:
        out["changes"] = [
            {"path": c.path, "rel": relative(c.path, cwd), "kind": c.kind, "line": _first_added(c)} if links
            else _change(c, cwd, blamed_at if c.path == blamed_path else None)
            for c in event.file_changes]
    return out


def _change(change: FileChange, cwd: str | None, mark: int | None) -> dict[str, Any]:
    """A file change as diff lines (`+`/`-`/` `/`@@` prefixed), with the blamed line's index when marked."""
    if change.kind == model.DELETE:
        lines: list[str] = []
    else:
        raw = change.diff or ""
        lines = ["+" + text for text in raw.splitlines()] if change.kind == model.ADD else raw.splitlines()
    marked = added_line_at(lines, mark) if mark is not None else None
    start = 0
    if len(lines) > DIFF_LIMIT:
        start = max(0, min((marked or 0) - DIFF_LIMIT // 2, len(lines) - DIFF_LIMIT))
    shown = lines[start:start + DIFF_LIMIT]
    return {"path": change.path, "rel": relative(change.path, cwd), "kind": change.kind,
            "observed": change.observed, "lines": shown, "marked": None if marked is None else marked - start,
            "cut_before": start, "cut_after": len(lines) - start - len(shown)}


def _observed(store: Store, observation_id: int, path: str, born: int | None) -> list[dict[str, Any]]:
    return [_change(c, None, born if c.path == path else None) for c in observed_file_changes(store, observation_id)]


def _first_added(change: FileChange) -> int | None:
    """The first line an edit added, in the file right after it: where a link to it should land."""
    if change.kind == model.ADD:
        return 1
    number = None
    for text in (change.diff or "").splitlines():
        header = HUNK_HEADER.match(text)
        if header:
            number = int(header[3])
        elif number is not None:
            if text.startswith("+"):
                return number
            if not text.startswith(("-", "\\")):
                number += 1
    return None


# -- the server ---------------------------------------------------------------------------------------

@dataclass
class Sync:
    """The background sync, as the page sees it through /api/status."""
    running: bool = False
    syncs: int = 0
    ingested: int = 0  # by the last sync that found something
    ingested_at: int | None = None
    failed: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def loop(self, db: str, stop: threading.Event, every: float = SYNC_EVERY) -> None:
        while not stop.is_set():
            self.run(db)
            stop.wait(every)

    def run(self, db: str) -> None:
        with self.lock:
            self.running = True
        store = Store(db)
        try:
            results = sources.sync(store)
        except Exception as error:  # the viewer still shows what's already stored
            results = []
            self.failed = [f"sync: {error}"]
        else:
            self.failed = [f"{r.path}: {r.reason}" for r in results if r.status == sources.FAILED]
        finally:
            store.close()
        ingested = sum(r.status == sources.INGESTED for r in results)
        with self.lock:
            if ingested:
                self.ingested, self.ingested_at = ingested, int(time.time() * 1000)
            self.syncs += 1
            self.running = False


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, db: str, static: Path = STATIC) -> None:
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except OSError:  # taken: any free port will do
            super().__init__(("127.0.0.1", 0), Handler)
        self.db, self.static, self.sync = db, static, Sync()
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.cache: dict[tuple, dict] = {}
        self.cache_lock = threading.Lock()

    def url(self, target: str | None = None) -> str:
        base = f"http://127.0.0.1:{self.port}/"
        if not target:
            return base
        path, _, number = target.rpartition(":")
        if not (path and number.isdigit()):
            path, number = target, ""
        hash_ = f"#/file/{quote(os.path.abspath(path), safe='')}"
        return base + hash_ + (f"?line={number}" if number else "")


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # quiet: the terminal stays clean
        pass

    def do_GET(self) -> None:
        if self.headers.get("Host") not in self.server.hosts:
            return self._send(403, b"forbidden", "text/plain")
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            return self._api(url.path[len("/api/"):], {k: v[0] for k, v in parse_qs(url.query).items()})
        return self._static(url.path)

    def _api(self, route: str, query: dict[str, str]) -> None:
        server = self.server
        try:
            if route == "status":
                sync = server.sync
                with sync.lock:
                    data: Any = {"syncing": sync.running and not sync.syncs, "syncs": sync.syncs,
                                 "ingested": sync.ingested, "ingested_at": sync.ingested_at, "failed": sync.failed,
                                 "db": server.db, "version": _mtime(server.db)}  # changes with every write
            elif route == "files":
                data = self._read(files)
            elif route == "blame":
                path = _required(query, "path")
                key = (path, _mtime(server.db))
                with server.cache_lock:
                    data = server.cache.get(key)
                if data is None:
                    data = self._read(lambda store: blame_view(store, path))
                    with server.cache_lock:
                        server.cache = {k: v for k, v in server.cache.items() if k[1:] == key[1:]}
                        server.cache[key] = data
            elif route == "story":
                path, line = _required(query, "path"), _required(query, "line")
                if not line.isdigit():
                    raise ValueError("line must be a number")
                data = self._read(lambda store: story_view(store, path, int(line)))
            elif route == "sessions":
                data = self._read(sessions_view)
            elif route.startswith("sessions/"):
                session = unquote(route[len("sessions/"):])
                data = self._read(lambda store: session_view(store, session))
            else:
                raise LookupError(f"no such API: {route}")
        except LookupError as error:
            return self._json(404, {"error": error.args[0]})
        except ValueError as error:
            return self._json(400, {"error": error.args[0]})
        self._json(200, data)

    def _read(self, query: Callable[[Store], Any]) -> Any:
        store = Store(self.server.db)  # sqlite connections stay on the thread that opened them
        try:
            return query(store)
        finally:
            store.close()

    def _static(self, path: str) -> None:
        root = self.server.static.resolve()
        name = "index.html" if path in ("", "/") else unquote(path).lstrip("/")
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            if not (root / "index.html").is_file():
                return self._send(404, b"The viewer isn't built: run `npm ci && npm run build` in web/.",
                                  "text/plain")
            return self._send(404, b"not found", "text/plain")
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
            kind += "; charset=utf-8"
        cache = IMMUTABLE if name.startswith("assets/") else "no-cache"
        self._send(200, target.read_bytes(), kind, cache)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, separators=(",", ":")).encode(), "application/json", "no-store")

    def _send(self, status: int, body: bytes, kind: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def _required(query: dict[str, str], name: str) -> str:
    if not query.get(name):
        raise ValueError(f"missing ?{name}=")
    return query[name]


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def built(static: Path = STATIC) -> bool:
    return (static / "index.html").is_file()


def run(server: Server, target: str | None, open_browser: bool) -> int:
    """Serve until interrupted. Sync runs in the background so the page opens at once."""
    stop = threading.Event()
    threading.Thread(target=server.sync.loop, args=(server.db, stop), daemon=True).start()
    url = server.url(target)
    print(f"ah view → {url}", flush=True)
    if not built(server.static):
        print("  (the page isn't built; the API is up. Build it with `npm ci && npm run build` in web/.)")
    print("  Ctrl-C to stop.", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0
