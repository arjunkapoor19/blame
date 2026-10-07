"""Claude Code transcripts. Records are built in the exact shapes found in real transcripts."""

import json
import shutil
from pathlib import Path

import pytest

from agent_history import model, sources
from agent_history.adapters import claude_code, codex
from agent_history.adapters.claude_code import looks_like_transcript, normalize
from agent_history.blame import Line, apply_hunks, blame, story
from agent_history.cli import main
from agent_history.commands import summarize
from agent_history.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
SHOP_CAPTURE = FIXTURES / "codex" / "20261004T132159Z-39de3d"  # Codex fixed apply_discount and average
SESSION = "11111111-1111-4111-8111-111111111111"
COMMON = {"sessionId": SESSION, "cwd": "/workspace", "version": "2.1.288", "gitBranch": "main",
          "isSidechain": False, "userType": "external", "entrypoint": "cli"}

MEDIAN_HUNK = {"oldStart": 10, "oldLines": 3, "newStart": 10, "newLines": 7, "lines": [
    "     if not prices:", "         return 0.0", "     return sum(prices) / len(prices)",
    "+", "+", "+def median(prices):", "+    return sorted(prices)[len(prices) // 2]"]}
TEST_FILE = "import unittest\n\nfrom shop import median\n\n\nclass MedianTests(unittest.TestCase):\n" \
            "    def test_odd(self):\n        self.assertEqual(median([3, 1, 2]), 2)\n"
FAILED_RUN = "Exit code 1\nE\n======\nERROR: test_odd\nImportError: cannot import name 'median' from 'shop'\n" \
             "------\nRan 1 test in 0.001s\n\nFAILED (errors=1)"


class Transcript:
    """Writes Claude Code records the way Claude Code does."""

    def __init__(self, session=SESSION):
        self.records, self.n, self.session = [], 0, session

    def _uuid(self):
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"

    def _rec(self, kind, at, **fields):
        record = {**COMMON, "sessionId": self.session, "type": kind, "uuid": self._uuid(),
                  "timestamp": f"2026-10-06T10:{at}.000Z", **fields}
        self.records.append(record)
        return record

    def bookkeeping(self):
        self.records.append({"type": "permission-mode", "permissionMode": "default", "sessionId": self.session})
        self.records.append({"type": "file-history-snapshot", "messageId": "m0", "snapshot": {}})

    def prompt(self, at, text, prompt_id, **fields):
        self._rec("user", at, promptId=prompt_id, message={"role": "user", "content": text}, **fields)

    def reply(self, at, message_id, blocks, stop_reason=None, output_tokens=50):
        """One API reply, split one block per record, each repeating the same usage."""
        usage = {"input_tokens": 3, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200,
                 "output_tokens": output_tokens, "output_tokens_details": {"thinking_tokens": 10}}
        for block in blocks:
            self._rec("assistant", at, message={
                "id": message_id, "model": "claude-opus-5-5", "role": "assistant", "content": [block],
                "stop_reason": stop_reason if block is blocks[-1] else None, "usage": usage})

    def result(self, at, tool_id, prompt_id, content, tool_result, is_error=None):
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        if is_error is not None:
            block["is_error"] = is_error
        self._rec("user", at, promptId=prompt_id, toolUseResult=tool_result,
                  message={"role": "user", "content": [block]})

    def write(self, path):
        path.write_text("\n".join(json.dumps(r) for r in self.records) + "\n")
        return path


def tool(tool_id, name, **args):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def text(value):
    return {"type": "text", "text": value}


def median_session(t: Transcript) -> Transcript:
    """Turn 1: add median() with a test (fails, then passes). Turn 2: an edit is rejected, then interrupted."""
    t.bookkeeping()
    t.prompt("00:00", "median() is missing. Add it with a test.", "p1")
    t.reply("00:01", "msg_1", [{"type": "thinking", "thinking": "Look first.", "signature": "x"},
                               text("I'll check shop.py, add a test, then add median()."),
                               tool("t_read", "Read", file_path="/workspace/shop.py")])
    t.result("00:02", "t_read", "p1", "1\tdef total(prices):…",
             {"type": "text", "file": {"filePath": "/workspace/shop.py", "content": "…", "numLines": 12}})
    t.reply("00:03", "msg_2", [tool("t_write", "Write", file_path="/workspace/test_median.py", content=TEST_FILE)])
    t.result("00:04", "t_write", "p1", "File created successfully at: /workspace/test_median.py",
             {"type": "create", "filePath": "/workspace/test_median.py", "content": TEST_FILE,
              "structuredPatch": [], "originalFile": None, "userModified": False})
    t.reply("00:05", "msg_3", [tool("t_bash1", "Bash", command="python3 -m unittest -q", description="Run tests")])
    t.result("00:06", "t_bash1", "p1", FAILED_RUN, "Error: " + FAILED_RUN, is_error=True)
    t.reply("00:07", "msg_4", [text("The test fails because median() doesn't exist yet. Adding it."),
                               tool("t_edit", "Edit", file_path="/workspace/shop.py",
                                    old_string="    return sum(prices) / len(prices)\n", new_string="…")])
    t.result("00:08", "t_edit", "p1", "The file /workspace/shop.py has been updated successfully.",
             {"filePath": "/workspace/shop.py", "oldString": "…", "newString": "…", "originalFile": None,
              "structuredPatch": [MEDIAN_HUNK], "userModified": False, "replaceAll": False})
    t.reply("00:09", "msg_5", [tool("t_bash2", "Bash", command="python3 -m unittest -q")])
    t.result("00:10", "t_bash2", "p1", "Ran 1 test in 0.000s\n\nOK",
             {"stdout": "", "stderr": "----\nRan 1 test in 0.000s\n\nOK\n", "interrupted": False})
    t.reply("00:11", "msg_6", [text("Added median() and a test; it passes.")], stop_reason="end_turn")
    t.prompt("00:12", "<command-name>/model</command-name>", "p1b")  # a slash command, not a turn
    t.prompt("00:13", "## Context Usage", "p1c", isMeta=True)  # its output, not a turn
    t.prompt("00:14", "Now make median() average the two middle values.", "p2")
    t.reply("00:15", "msg_7", [tool("t_edit2", "Edit", file_path="/workspace/shop.py",
                                    old_string="x", new_string="y")])
    t.result("00:16", "t_edit2", "p2", "The user doesn't want to proceed with this tool use. The tool use was "
             "rejected (eg. if it was a file edit, the new_string was NOT written to the file).",
             "User rejected tool use", is_error=True)
    t.prompt("00:17", "[Request interrupted by user for tool use]", "p2")
    return t


@pytest.fixture
def transcript(tmp_path):
    return median_session(Transcript()).write(tmp_path / f"{SESSION}.jsonl")


def test_session_and_turns(transcript):
    n = normalize(transcript)
    assert (n.session.id, n.session.agent, n.session.agent_version, n.session.cwd) == (
        SESSION, "claude-code", "2.1.288", "/workspace")  # taken from past the bookkeeping records
    assert [(t.seq, t.status, t.input_text) for t in n.turns] == [
        (1, "completed", "median() is missing. Add it with a test."),
        (2, "interrupted", "Now make median() average the two middle values.")]
    assert n.session.outcome == "interrupted"
    assert [t.id for t in n.threads] == ["00000000-0000-4000-8000-000000000001"]  # the conversation's root


def test_events(transcript):
    n = normalize(transcript)
    by_kind = lambda kind: [e for e in n.events if e.kind == kind]
    commands = by_kind(model.COMMAND)
    assert [(c.status, c.payload["exit_code"]) for c in commands] == [("failed", 1), ("completed", 0)]
    assert summarize(commands[0].payload, commands[0].status) == \
        "1 test, 1 errored · ImportError: cannot import name 'median' from 'shop'"
    assert summarize(commands[1].payload, commands[1].status) == "1 test passed"

    edits = by_kind(model.FILE_CHANGE)
    assert [(e.status, [(c.path, c.kind) for c in e.file_changes]) for e in edits] == [
        ("completed", [("/workspace/test_median.py", "add")]),
        ("completed", [("/workspace/shop.py", "update")]),
        ("declined", [])]

    [read] = by_kind(model.TOOL_CALL)
    assert read.payload["actions"] == [{"type": "read", "path": "/workspace/shop.py", "command": None}]
    assert "result" not in read.payload  # file contents aren't copied into the store

    replies = by_kind(model.AGENT_MESSAGE)
    assert [r.payload["phase"] for r in replies] == ["commentary", "commentary", "final"]
    assert len(by_kind(model.REASONING)) == 1


def test_token_usage_is_counted_once_per_reply(transcript):
    usage = [e for e in normalize(transcript).events if e.kind == model.TOKEN_USAGE]
    assert len(usage) == 7  # 7 replies, though msg_1 and msg_4 span several records
    assert usage[-1].payload["total"]["output"] == 7 * 50
    assert usage[0].payload["last"] == {"input": 1203, "cached_input": 1000, "output": 50,
                                        "reasoning_output": 10, "total": 1253}
    assert usage[0].payload["model"] == "claude-opus-5-5"


def test_whole_file_deletion_hunk_replays():
    # Recorded by Claude Code when an Edit removed a file's entire content.
    hunk = {"oldStart": 1, "oldLines": 4, "newStart": 1, "newLines": 0,
            "lines": ["-keep 1", "-keep 2", "-drop a", "-drop b"]}
    lines = [Line(t) for t in ["keep 1", "keep 2", "drop a", "drop b"]]
    apply_hunks(lines, claude_code._unified([hunk]), "e")
    assert lines == []


def test_two_agents_one_file(tmp_path, transcript):
    """Codex fixed shop.py; then Claude Code added median(). Blame tells them apart."""
    store = Store(tmp_path / "h.db")
    store.ingest(codex.normalize(SHOP_CAPTURE))
    store.ingest(normalize(transcript))
    lines = blame(store, "/workspace/shop.py").lines
    agents = {l.number: store.find_session(l.origin.session_id)["agent"] for l in lines if l.origin}
    assert agents == {6: "codex", 10: "codex", 11: "codex",
                      13: "claude-code", 14: "claude-code", 15: "claude-code", 16: "claude-code"}

    told = story(store, lines[14].origin)
    assert told.prompt == "median() is missing. Add it with a test."
    origin_at = told.steps.index(lines[14].origin)
    assert told.steps[origin_at - 1].payload["text"].startswith("The test fails because")
    assert told.steps[origin_at + 1].payload["exit_code"] == 0


def test_cli_story_marks_the_exact_line(tmp_path, transcript, capsys):
    db = str(tmp_path / "h.db")
    assert main(["--db", db, "ingest", str(SHOP_CAPTURE), str(transcript)]) == 0
    capsys.readouterr()
    assert main(["--db", db, "blame", "/workspace/shop.py:15"]) == 0
    out = capsys.readouterr().out
    assert "written by claude-code" in out
    assert "  read     shop.py\n" in out
    assert "test     ✗ python3 -m unittest -q  → 1 test, 1 errored · ImportError" in out
    detail = " " * 21 + "│ "
    assert detail + "+def median(prices):   ← this line\n" in out
    assert out.count("   ← this line") == 1  # exactly one diff line is marked
    # Blank added lines just above don't steal the mark.
    assert detail + "+\n" in out


def test_resumed_conversation_is_stored_once(tmp_path, monkeypatch):
    """Resuming copies the transcript into a new session file; whichever is synced first, the
    later, longer copy is kept."""
    home = tmp_path / "claude"
    project = home / "projects" / "-workspace"
    project.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    original = median_session(Transcript())
    resumed = Transcript(session="22222222-2222-4222-8222-222222222222")
    resumed.records = [{**r, "sessionId": resumed.session} if "sessionId" in r else r for r in original.records]
    resumed.n = original.n
    resumed.prompt("05:00", "Thanks, that's all.", "p3")
    resumed.reply("05:01", "msg_8", [text("You're welcome.")], stop_reason="end_turn")

    for first, second in ((original, resumed), (resumed, original)):
        store = Store(tmp_path / f"{first.session}.db")
        for t in (first, second):
            t.write(project / f"{t.session}.jsonl")
            sources.sync(store)
        assert [s["id"] for s in store.sessions()] == [resumed.session]
        assert len(store.turns(resumed.session)) == 3
        for path in project.iterdir():
            path.unlink()


def test_detection(tmp_path, transcript):
    assert looks_like_transcript(transcript)
    assert not looks_like_transcript(FIXTURES / "codex" / "20261004T132159Z-39de3d" / "events.jsonl")
    rollout = next((FIXTURES / "codex_rollout").glob("rollout-2026-10-04*.jsonl"))
    assert sources.ingest(Store(tmp_path / "h.db"), rollout).source == "codex-log"


def test_sync_discovers_transcripts(tmp_path, monkeypatch, transcript):
    home = tmp_path / "claude-home"
    (home / "projects" / "-workspace").mkdir(parents=True)
    shutil.copy(transcript, home / "projects" / "-workspace")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    [result] = sources.sync(Store(tmp_path / "h.db"))
    assert (result.status, result.source, result.session_id) == ("ingested", "claude-code-log", SESSION)
