"""Blame with observed workspaces: shell edits, outside edits, baselines, several agents."""

import json
import subprocess
from datetime import datetime

import pytest

from agent_history.adapters import claude_code
from agent_history.blame import blame, story
from agent_history.cli import main
from agent_history.hooks import handle
from agent_history.store import Store

SESSION = "33333333-3333-4333-8333-333333333333"
APPEND = "printf '\\n\\ndef divide(a, b):\\n    return a / b\\n' >> shop.py"


def ms(clock: str) -> int:
    return int(datetime.fromisoformat(f"2026-10-06T{clock}+00:00").timestamp() * 1000)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "shop.py").write_text("def total(prices):\n    return sum(prices)\n")
    return path.resolve()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db" / "h.db")


def transcript(tmp_path, repo, calls):
    """A Claude Code transcript whose turn makes these (tool id, tool, input, result) calls."""
    common = {"sessionId": SESSION, "cwd": str(repo), "version": "2.1.288", "isSidechain": False}
    records, n = [], 0

    def rec(kind, clock, **fields):
        nonlocal n
        n += 1
        records.append({**common, "type": kind, "uuid": f"u{n}", "timestamp": f"2026-10-06T{clock}Z", **fields})

    rec("user", "10:00:00.000", promptId="p1", message={"role": "user", "content": "Add divide() to shop.py"})
    for i, (tool_id, name, args, result, clock) in enumerate(calls):
        rec("assistant", clock, message={"id": f"m{i}", "role": "assistant", "model": "m", "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": args}], "stop_reason": None})
        rec("user", clock, promptId="p1", toolUseResult=result,
            message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]})
    rec("assistant", "10:09:00.000", message={"id": "mz", "role": "assistant", "model": "m", "content": [
        {"type": "text", "text": "Done."}], "stop_reason": "end_turn"})
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def hook(store, repo, event, tool_id, clock, agent="claude-code", session=SESSION, command=APPEND):
    payload = {"session_id": session, "cwd": str(repo), "tool_name": "Bash", "tool_use_id": tool_id,
               "tool_input": {"command": command}}
    handle(store, agent, event, payload, now=ms(clock))


def shell(repo, command):
    subprocess.run(command, shell=True, cwd=repo, check=True)


def test_a_shell_edit_is_attributed_to_its_command(tmp_path, repo, store, capsys):
    hook(store, repo, "pre", "toolu_sh", "10:01:00")
    shell(repo, APPEND)
    hook(store, repo, "post", "toolu_sh", "10:01:01")
    store.ingest(claude_code.normalize(transcript(tmp_path, repo, [
        ("toolu_sh", "Bash", {"command": APPEND}, {"stdout": "", "stderr": "", "interrupted": False},
         "10:01:00.500")])))

    lines = blame(store, str(repo / "shop.py")).lines
    assert [(l.text, l.author.kind if l.author else None) for l in lines] == [
        ("def total(prices):", "baseline"), ("    return sum(prices)", "baseline"),
        ("", "agent"), ("", "agent"), ("def divide(a, b):", "agent"), ("    return a / b", "agent")]
    divide = lines[4]
    assert divide.origin.kind == "command" and divide.origin.payload["command"] == APPEND
    assert divide.author.agent == "claude-code" and divide.author.observation["tool_call_id"] == "toolu_sh"
    [observed] = story(store, divide.origin).steps[0].file_changes
    assert observed.observed and "+def divide(a, b):" in observed.diff

    db = str(tmp_path / "db" / "h.db")
    assert main(["--db", db, "blame", f"{repo}/shop.py:5"]) == 0
    out = capsys.readouterr().out
    assert "written by claude-code" in out and "observed by ah" in out
    assert "command  ✓ printf" in out
    assert " " * 21 + "changed shop.py:\n" in out
    assert " " * 21 + "│ +def divide(a, b):   ← this line\n" in out

    assert main(["--db", db, "blame", f"{repo}/shop.py"]) == 0
    out = capsys.readouterr().out
    header, rows = out.splitlines()[0], out.splitlines()[1:]
    assert header.split() == ["author", "session", "turn", "time", "#", "code"]
    assert rows[0].startswith("·       ")
    assert rows[4].startswith("claude  333333  t1 ")
    # Column titles sit over their columns.
    assert header.index("session") == rows[4].index("333333")
    assert header.index("turn") == rows[4].index("t1")
    assert header.index("#") == rows[4].index("5  def divide") 
    assert "4 claude · 2 pre-existing" in out


def test_edits_outside_any_agent_are_named(tmp_path, repo, store, capsys):
    hook(store, repo, "pre", "t1", "10:01:00")
    hook(store, repo, "post", "t1", "10:01:01")
    (repo / "shop.py").write_text("def total(prices):\n    return sum(prices)  # by hand\n")
    hook(store, repo, "pre", "t2", "10:05:00")
    line = blame(store, str(repo / "shop.py")).lines[1]
    assert line.author.kind == "outside" and line.origin is None
    assert main(["--db", str(tmp_path / "db" / "h.db"), "blame", f"{repo}/shop.py:2"]) == 0
    out = capsys.readouterr().out
    assert "Edited outside any agent (by you or another program) between" in out
    assert "│ +    return sum(prices)  # by hand   ← this line" in out


def test_observation_replaces_the_agents_own_report(tmp_path, repo, store):
    """An Edit is both reported (structuredPatch) and observed: applied once, not twice."""
    patch = [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3,
              "lines": [" def total(prices):", "-    return sum(prices)", "+    if not prices:",
                        "+        return 0", "+    return sum(prices)"]}]
    hook(store, repo, "pre", "toolu_edit", "10:01:00", command=str(repo / "shop.py"))
    (repo / "shop.py").write_text("def total(prices):\n    if not prices:\n        return 0\n    return sum(prices)\n")
    hook(store, repo, "post", "toolu_edit", "10:01:01", command=str(repo / "shop.py"))
    store.ingest(claude_code.normalize(transcript(tmp_path, repo, [
        ("toolu_edit", "Edit", {"file_path": str(repo / "shop.py")},
         {"filePath": str(repo / "shop.py"), "structuredPatch": patch}, "10:01:00.500")])))
    lines = blame(store, str(repo / "shop.py")).lines
    assert [l.text for l in lines] == ["def total(prices):", "    if not prices:", "        return 0",
                                       "    return sum(prices)"]
    assert [l.author.kind for l in lines] == ["baseline", "agent", "agent", "baseline"]
    assert lines[1].origin.kind == "file_change"


def test_history_from_before_observation_is_kept(tmp_path, repo, store):
    """Lines an agent wrote before hooks were installed keep their author through the baseline."""
    (repo / "shop.py").unlink()
    store.ingest(claude_code.normalize(transcript(tmp_path, repo, [
        ("toolu_w", "Write", {"file_path": str(repo / "shop.py")},
         {"type": "create", "filePath": str(repo / "shop.py"), "content": "def total(prices):\n"},
         "10:00:30.000")])))
    (repo / "shop.py").write_text("def total(prices):\n")
    hook(store, repo, "pre", "later", "10:30:00")  # first observation: the baseline
    shell(repo, "echo '    return sum(prices)' >> shop.py")
    hook(store, repo, "post", "later", "10:30:01", command="echo … >> shop.py")
    lines = blame(store, str(repo / "shop.py")).lines
    assert [(l.author.kind, l.origin.kind if l.origin else None) for l in lines] == [
        ("agent", "file_change"),  # the recorded Write, from before observation began
        ("agent", None)]  # observed, but this session's transcript doesn't include that call


def test_two_agents_one_file(tmp_path, repo, store, capsys):
    hook(store, repo, "pre", "c1", "10:01:00", agent="codex", session="codex-thread")
    shell(repo, "echo 'def mean(xs): return sum(xs) / len(xs)' >> shop.py")
    hook(store, repo, "post", "c1", "10:01:01", agent="codex", session="codex-thread")
    hook(store, repo, "pre", "toolu_sh", "10:02:00")
    shell(repo, "sed -i.bak 's|sum(xs) / len(xs)|sum(xs) / max(len(xs), 1)|' shop.py && rm shop.py.bak")
    hook(store, repo, "post", "toolu_sh", "10:02:01")
    lines = blame(store, str(repo / "shop.py")).lines
    mean = lines[2]
    assert mean.author.agent == "claude-code"
    assert [(who.agent, text) for who, text in mean.history] == [
        ("codex", "def mean(xs): return sum(xs) / len(xs)"),
        ("claude-code", "def mean(xs): return sum(xs) / max(len(xs), 1)")]
    assert main(["--db", str(tmp_path / "db" / "h.db"), "blame", f"{repo}/shop.py:3"]) == 0
    out = capsys.readouterr().out
    assert "written by claude-code" in out and "transcript hasn't been synced yet" in out


def test_concurrent_calls_are_flagged(tmp_path, repo, store, capsys):
    hook(store, repo, "pre", "a", "10:01:00")
    hook(store, repo, "pre", "b", "10:01:00", agent="codex", session="codex-thread")
    shell(repo, "echo 'x = 1' >> shop.py")
    hook(store, repo, "post", "b", "10:01:01", agent="codex", session="codex-thread")
    hook(store, repo, "post", "a", "10:01:02")
    line = blame(store, str(repo / "shop.py")).lines[-1]
    assert line.author.concurrent
    assert main(["--db", str(tmp_path / "db" / "h.db"), "blame", f"{repo}/shop.py"]) == 0
    assert "≈ written while another tool call was running" in capsys.readouterr().out
