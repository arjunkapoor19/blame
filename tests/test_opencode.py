"""opencode sessions. Rows are built in the exact shapes found in a real opencode database."""

import copy
import itertools
import json
import sqlite3
from pathlib import Path

import pytest

from agent_history import model, sources
from agent_history.adapters.opencode import _file_changes, connect, normalize
from agent_history.blame import Line, apply_hunks, blame
from agent_history.cli import main
from agent_history.store import Store

SESSION = "ses_1111111111111111111111"
SHOP = "def total(prices):\n    return sum(prices)\n"
SHOP_AFTER = "def total(prices):\n    return sum(prices)\n\n\ndef median(prices):\n" \
             "    return sorted(prices)[len(prices) // 2]\n"
TEST_FILE = "from shop import median\n\nassert median([3, 1, 2]) == 2\n"


class Database:
    """Writes opencode's session, message and part rows the way opencode does."""

    def __init__(self, path: Path, session=SESSION, cwd="/workspace"):
        self.path, self.session, self.cwd, self.ids = path, session, cwd, itertools.count(1)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, slug TEXT,
                directory TEXT, title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
                time_updated INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT);""")
        self._session(None, 0)

    def child(self, session, second):
        """A sub-agent's session: opencode's `task` tool runs one with `parent_id` set."""
        sub = copy.copy(self)  # shares the connection and the id counter
        sub.session = session
        sub._session(self.session, second)
        return sub

    def _session(self, parent, second):
        self.db.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
                        (self.session, "proj", parent, "slug", self.cwd, "Add median", "1.18.30", at(second),
                         at(second)))

    def _id(self, prefix):
        return f"{prefix}_{next(self.ids):012d}"

    def prompt(self, second, text):
        message = self._id("msg")
        self._message(message, second, {"role": "user", "time": {"created": at(second)}, "agent": "build",
                                        "model": {"providerID": "openai", "modelID": "gpt-5"}})
        self._part(message, second, {"type": "text", "text": text})
        self._part(message, second, {"type": "text", "text": "<file contents>", "synthetic": True})
        return message

    def reply(self, second, parent, parts, finish="stop", error=None):
        message = self._id("msg")
        data = {"role": "assistant", "time": {"created": at(second), "completed": at(second + 5)},
                "parentID": parent, "modelID": "gpt-5", "providerID": "openai", "mode": "build",
                "path": {"cwd": self.cwd, "root": self.cwd}, "finish": finish}
        if error:
            data["error"] = error
        self._message(message, second, data)
        self._part(message, second, {"type": "step-start", "snapshot": "abc"})
        for part in parts:
            self._part(message, second, part)
        self._part(message, second, {"type": "step-finish", "reason": finish, "snapshot": "abc", "cost": 0,
                                     "tokens": {"total": 1300, "input": 100, "output": 200, "reasoning": 10,
                                                "cache": {"read": 1000, "write": 0}}})

    def touch(self, second):
        self.db.execute("UPDATE session SET time_updated = ? WHERE id = ?", (at(second), self.session))
        self.db.commit()

    def save(self):
        self.db.commit()
        return self.path / self.session

    def _message(self, message, second, data):
        self.db.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                        (message, self.session, at(second), at(second), json.dumps(data)))

    def _part(self, message, second, data):
        self.db.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                        (self._id("prt"), message, self.session, at(second), at(second), json.dumps(data)))


def at(second):
    return 1_791_000_000_000 + second * 1000


def tool(call, name, status, start, args, output=None, metadata=None, error=None):
    state = {"status": status, "input": args, "title": "", "metadata": metadata or {},
             "time": {"start": at(start), "end": at(start + 1)}}
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    return {"type": "tool", "callID": call, "tool": name, "state": state}


def text(value):
    return {"type": "text", "text": value}


def shop_session(path, cwd="/workspace"):
    """Turn 1 adds median() with tests; turn 2 is aborted; turn 3 hits an API error."""
    db = Database(path, cwd=cwd)
    first = db.prompt(1, "Add median() with a test.")
    db.reply(2, first, [
        text("Let me look at shop.py."),
        tool("call_read", "read", "completed", 2, {"filePath": f"{cwd}/shop.py"}, "<file>"),
        tool("call_edit", "edit", "completed", 3,
             {"filePath": f"{cwd}/shop.py", "oldString": "x", "newString": "y"}, "Edit applied successfully.",
             # `diff` strips common indentation (it's for display); `filediff` holds the real contents.
             {"diff": "Index: shop.py\n===\n--- shop.py\n+++ shop.py\n@@ -1,2 +1,6 @@\n def total(prices):\n"
                      "return sum(prices)\n+\n+\n+def median(prices):\n+return sorted(prices)[len(prices) // 2]\n",
              "filediff": {"file": f"{cwd}/shop.py", "before": SHOP, "after": SHOP_AFTER}}),
        tool("call_write", "write", "completed", 4, {"filePath": f"{cwd}/test_shop.py", "content": TEST_FILE},
             "Wrote file successfully.", {"filepath": f"{cwd}/test_shop.py", "exists": False}),
        tool("call_fail", "bash", "completed", 5, {"command": "python3 -m pytest -q", "description": "Run tests"},
             "1 failed", {"output": "1 failed", "exit": 1}),
        tool("call_ok", "bash", "completed", 6, {"command": "python3 test_shop.py", "description": "Run it"},
             "", {"output": "", "exit": 0}),
        tool("call_no", "edit", "error", 7, {"filePath": f"{cwd}/README.md"},
             error="Error: The user rejected permission to use this specific tool call."),
        text("Added median() and a test."),
    ])
    second = db.prompt(20, "Now handle empty lists.")
    db.reply(21, second, [tool("call_hang", "bash", "running", 21, {"command": "sleep 100"})],
             finish=None, error={"name": "MessageAbortedError", "data": {"message": "Aborted"}})
    third = db.prompt(30, "hi")
    db.reply(31, third, [], finish=None,
             error={"name": "APIError", "data": {"message": "The requested model is not supported."}})
    return db.save()


@pytest.fixture
def session_path(tmp_path):
    return shop_session(tmp_path / "opencode.db")


def test_session_and_turns(session_path):
    n = normalize(session_path)
    assert (n.session.id, n.session.agent, n.session.agent_version, n.session.cwd) == \
        (SESSION, "opencode", "1.18.30", "/workspace")
    assert [(t.seq, t.input_text, t.status) for t in n.turns] == [
        (1, "Add median() with a test.", "completed"), (2, "Now handle empty lists.", "interrupted"),
        (3, "hi", "failed")]
    assert n.session.outcome == "failed"
    assert [t.id for t in n.threads] == [SESSION]


def test_events(session_path):
    events = normalize(session_path).events
    by_call = {e.payload.get("source_id"): e for e in events if e.payload.get("source_id")}
    read = by_call["call_read"]
    assert read.kind == model.TOOL_CALL and read.payload["actions"][0] == \
        {"type": model.READ, "path": "/workspace/shop.py", "command": None}
    assert "result" not in read.payload  # file contents aren't kept

    edit = by_call["call_edit"].file_changes[0]
    assert (edit.path, edit.kind) == ("/workspace/shop.py", model.UPDATE)
    assert "+    return sorted(prices)[len(prices) // 2]" in edit.diff  # indentation intact

    created = by_call["call_write"].file_changes[0]
    assert (created.path, created.kind, created.diff) == ("/workspace/test_shop.py", model.ADD, TEST_FILE)

    assert (by_call["call_fail"].payload["exit_code"], by_call["call_fail"].payload["output"]) == (1, "1 failed")
    assert by_call["call_fail"].status == "failed"
    assert by_call["call_ok"].payload["exit_code"] == 0 and by_call["call_ok"].status == "completed"
    assert by_call["call_no"].status == "declined" and by_call["call_no"].file_changes == []
    assert by_call["call_hang"].status == "interrupted"

    replies = [e for e in events if e.kind == model.AGENT_MESSAGE]
    assert [r.payload["phase"] for r in replies] == ["commentary", "final"]
    assert all(e.payload.get("record_id") for e in events)
    assert [e.payload["message"] for e in events if e.kind == model.ERROR] == ["The requested model is not supported."]
    prompts = [e.payload["text"] for e in events if e.kind == model.USER_MESSAGE]
    assert "<file contents>" not in prompts[0]  # synthetic parts aren't the person's words


def test_token_usage_counts_cache(session_path):
    usage = [e.payload for e in normalize(session_path).events if e.kind == model.TOKEN_USAGE]
    assert usage[0]["last"] == {"input": 1100, "cached_input": 1000, "output": 200, "reasoning_output": 10,
                                "total": 1300}
    assert usage[-1]["total"]["total"] == 1300 * len(usage)


def test_blame_attributes_lines_to_opencode(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.py").write_text(SHOP_AFTER)
    path = shop_session(tmp_path / "opencode.db", cwd=str(repo))
    store = Store(tmp_path / "h.db")
    store.ingest(normalize(path), "opencode-db")
    lines = blame(store, str(repo / "shop.py")).lines
    assert [(l.text, l.author.agent if l.author else None) for l in lines][4:] == [
        ("def median(prices):", "opencode"), ("    return sorted(prices)[len(prices) // 2]", "opencode")]

    assert main(["--db", str(tmp_path / "h.db"), "blame", f"{repo}/shop.py:6"]) == 0
    out = capsys.readouterr().out
    assert "written by opencode" in out and 'You asked: "Add median() with a test."' in out
    assert "+    return sorted(prices)[len(prices) // 2]   ← this line" in out
    assert "test     ✗ python3 -m pytest -q" in out


def test_sync_discovers_sessions_and_skips_unchanged(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "opencode").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    db = Database(data / "opencode" / "opencode.db")
    db.reply(2, db.prompt(1, "hello"), [text("Hi!")])
    db.save()
    store = Store(tmp_path / "h.db")
    assert [(r.source, r.status, r.session_id) for r in sources.sync(store)] == \
        [("opencode-db", sources.INGESTED, SESSION)]
    assert sources.sync(store) == []
    db.reply(10, db.prompt(9, "again"), [text("Sure.")])
    db.touch(10)
    assert [r.status for r in sources.sync(store)] == [sources.INGESTED]
    assert len(normalize(data / "opencode" / "opencode.db" / SESSION).turns) == 2


def test_explicit_ingest(tmp_path, session_path):
    store = Store(tmp_path / "h.db")
    assert sources.ingest(store, session_path).status == sources.INGESTED
    with pytest.raises(LookupError):
        sources.ingest(store, tmp_path / "elsewhere" / SESSION)


CHILD = "ses_2222222222222222222222"


def delegating_session(path, cwd="/workspace"):
    """The agent hands the edit to a sub-agent, which runs as a child session and edits shop.py."""
    db = Database(path, cwd=cwd)
    prompt = db.prompt(1, "Add median(); use a sub-agent.")
    db.reply(2, prompt, [
        tool("call_task", "task", "completed", 2,
             {"description": "Add median", "prompt": "Add median() to shop.py", "subagent_type": "general"},
             "Added median().", {"sessionId": CHILD, "summary": []}),
        text("The sub-agent added median()."),
    ])
    sub = db.child(CHILD, 3)
    task = sub.prompt(3, "Add median() to shop.py")
    sub.reply(4, task, [
        tool("call_sub_edit", "edit", "completed", 4,
             {"filePath": f"{cwd}/shop.py", "oldString": "x", "newString": "y"}, "Edit applied successfully.",
             {"filediff": {"file": f"{cwd}/shop.py", "before": SHOP, "after": SHOP_AFTER}}),
        text("Done."),
    ])
    sub.touch(5)
    db.save()
    return db


def test_sub_agent_sessions_are_child_threads(tmp_path):
    database = tmp_path / "opencode.db"
    delegating_session(database)
    for path in (database / SESSION, database / CHILD):  # either id normalizes the whole run
        n = normalize(path)
        assert n.session.id == SESSION and n.session.source == str(database / SESSION)
        assert [(t.id, t.parent_thread_id) for t in n.threads] == [(SESSION, None), (CHILD, SESSION)]
        assert [(t.thread_id, t.seq, t.input_text) for t in n.turns] == [
            (SESSION, 1, "Add median(); use a sub-agent."), (CHILD, 1, "Add median() to shop.py")]
        assert n.session.outcome == "completed" and n.session.ended_at == at(5)
    events = normalize(database / SESSION).events
    task = next(e for e in events if e.kind == model.SUBAGENT_CALL)
    assert (task.thread_id, task.payload["receiver_thread_ids"]) == (SESSION, [CHILD])
    edit = next(e for e in events if e.kind == model.FILE_CHANGE)
    assert (edit.session_id, edit.thread_id, edit.file_changes[0].path) == (SESSION, CHILD, "/workspace/shop.py")
    assert {e.session_id for e in events} == {SESSION}


def test_sync_stores_one_session_and_follows_sub_agent_updates(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "opencode").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    db = delegating_session(data / "opencode" / "opencode.db")
    store = Store(tmp_path / "h.db")
    assert [(r.status, r.session_id) for r in sources.sync(store)] == [(sources.INGESTED, SESSION)]
    assert [row["id"] for row in store.sessions()] == [SESSION]
    assert sources.sync(store) == []
    sub = copy.copy(db)
    sub.session = CHILD
    sub.reply(9, sub.prompt(8, "Also add mode()"), [text("Added.")])
    sub.touch(9)  # only the child changed: the parent's run is re-read
    assert [(r.status, r.session_id) for r in sources.sync(store)] == [(sources.INGESTED, SESSION)]
    assert len(store.turns(SESSION)) == 3


def test_sub_agent_edits_blame_to_the_parent_session(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.py").write_text(SHOP_AFTER)
    delegating_session(tmp_path / "opencode.db", cwd=str(repo))
    store = Store(tmp_path / "h.db")
    store.ingest(normalize(tmp_path / "opencode.db" / SESSION), "opencode-db")
    line = blame(store, str(repo / "shop.py")).lines[5]
    assert line.author.agent == "opencode" and line.origin.thread_id == CHILD
    # A hook call from inside the sub-agent carries the child's sessionID; it still links.
    assert store.linked_event_id(CHILD, "call_sub_edit") == line.origin.id
    assert main(["--db", str(tmp_path / "h.db"), "blame", f"{repo}/shop.py:6"]) == 0
    out = capsys.readouterr().out
    assert f"session {SESSION} · turn 1 › sub-agent 22222222 ·" in out
    assert 'You asked: "Add median(); use a sub-agent."\n' \
           '  → handed to sub-agent 22222222: "Add median() to shop.py"' in out
    assert main(["--db", str(tmp_path / "h.db"), "log", SESSION]) == 0
    assert "turn 1  completed  6.0s  sub-agent 22222222, from turn 1\n" in capsys.readouterr().out


# A real session (sanitized): opencode delegated median() to a sub-agent, which found it already there;
# then, asked again, it added mean() with its edit tool, and both turns appended to shop.py from the shell.
DEMO_FIXTURE = Path(__file__).parent / "fixtures" / "opencode" / "ah-demo.sql"
DEMO = "ses_ed8dfd641ffeiswxPR4l6T6Mlw"
DEMO_CHILD = "ses_ed8df1b26ffe9k6XFnvV2ylcPq"


@pytest.fixture
def demo(tmp_path):
    database = tmp_path / "opencode.db"
    db = sqlite3.connect(database)
    db.executescript(DEMO_FIXTURE.read_text())
    db.close()
    return database / DEMO


def test_real_session(demo):
    n = normalize(demo)
    assert (n.session.agent, n.session.agent_version, n.session.cwd) == ("opencode", "1.18.30", "/workspace")
    assert n.session.outcome == "completed"
    assert [(t.id, t.parent_thread_id) for t in n.threads] == [(DEMO, None), (DEMO_CHILD, DEMO)]
    assert [(t.thread_id, t.seq, t.status) for t in n.turns] == [
        (DEMO, 1, "completed"), (DEMO_CHILD, 1, "completed"), (DEMO, 2, "completed")]
    [task] = [e for e in n.events if e.kind == model.SUBAGENT_CALL]
    assert (task.thread_id, task.payload["receiver_thread_ids"]) == (DEMO, [DEMO_CHILD])
    commands = [e.payload for e in n.events if e.kind == model.COMMAND]
    assert [c["command"] for c in commands if c["command"].startswith("echo")] == [
        'echo "# done" >> /workspace/shop.py'] * 2
    assert all(c["exit_code"] == 0 for c in commands)


def test_real_edit_uses_the_full_patch(demo):
    db = connect(demo.parent)
    [(data,)] = db.execute("SELECT data FROM part WHERE json_extract(data, '$.tool') = 'edit'")
    patch = json.loads(data)["state"]["metadata"]["filediff"]["patch"]
    [edit] = [e for e in normalize(demo).events if e.kind == model.FILE_CHANGE]
    [change] = edit.file_changes
    assert (change.path, change.kind) == ("/workspace/shop.py", model.UPDATE)
    assert change.diff == patch[patch.index("@@"):]
    assert "+def mean(prices):\n+    if not prices:\n" in change.diff


def test_full_patch_wins_over_the_display_diff():
    patch = "Index: /w/a.py\n--- /w/a.py\n+++ /w/a.py\n@@ -1,2 +1,2 @@\n     x = 1\n-    y = 2\n+    y = 3\n"
    shown = "Index: /w/a.py\n--- /w/a.py\n+++ /w/a.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"
    state = {"input": {"filePath": "/w/a.py"}, "metadata": {"diff": shown, "filediff": {"patch": patch}}}
    [change] = _file_changes("edit", state)
    lines = [Line(text) for text in ["    x = 1", "    y = 2"]]
    apply_hunks(lines, change.diff, "e")
    assert [line.text for line in lines] == ["    x = 1", "    y = 3"]
