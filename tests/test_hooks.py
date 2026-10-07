import json
import subprocess
from pathlib import Path

import pytest

from agent_history import model
from agent_history.hooks import handle, run
from agent_history.store import Store
from agent_history.workspace import Blobs


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "shop.py").write_text("def total(prices):\n    return sum(prices)\n")
    return path


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db" / "h.db")


def payload(repo, tool_id, command="", tool="Bash", session="s1", event="PreToolUse"):
    """Claude Code's hook input (Codex sends the same fields)."""
    return {"session_id": session, "transcript_path": "/t.jsonl", "cwd": str(repo), "hook_event_name": event,
            "tool_name": tool, "tool_input": {"command": command}, "tool_use_id": tool_id}


def observations(store, actor=None):
    rows = store.db.execute("SELECT * FROM observations" + (" WHERE actor = ?" if actor else "") + " ORDER BY id",
                            (actor,) if actor else ()).fetchall()
    return [dict(r) for r in rows]


def changes_of(store, observation_id):
    return [dict(r) for r in store.db.execute(
        "SELECT * FROM observed_changes WHERE observation_id = ? ORDER BY idx", (observation_id,))]


def shell(repo, command):
    subprocess.run(command, shell=True, cwd=repo, check=True)


def test_shell_edit_is_attributed_to_the_tool_call(repo, store):
    command = "printf '\\n\\ndef divide(a, b):\\n    return a / b\\n' >> shop.py"
    handle(store, "claude-code", "pre", payload(repo, "toolu_1", command), now=1000)
    shell(repo, command)
    handle(store, "claude-code", "post", payload(repo, "toolu_1", command, event="PostToolUse"), now=2000)

    [baseline] = observations(store, "baseline")
    assert [c["kind"] for c in changes_of(store, baseline["id"])] == [model.ADD]  # what was there before
    [call] = observations(store, "agent")
    assert (call["agent"], call["agent_session_id"], call["tool_call_id"], call["tool_name"]) == \
        ("claude-code", "s1", "toolu_1", "Bash")
    assert (call["started_at"], call["ended_at"], call["command"]) == (1000, 2000, command)
    [change] = changes_of(store, call["id"])
    assert (Path(change["path"]).name, change["kind"]) == ("shop.py", model.UPDATE)
    assert Blobs(store.objects_dir).text(change["after_blob"]).endswith("def divide(a, b):\n    return a / b\n")
    assert observations(store, "outside") == []


def test_edits_between_calls_happened_outside_the_agent(repo, store):
    handle(store, "claude-code", "pre", payload(repo, "t1"), now=1000)
    handle(store, "claude-code", "post", payload(repo, "t1"), now=1100)
    (repo / "shop.py").write_text("## a human comment\n" + (repo / "shop.py").read_text())
    handle(store, "claude-code", "pre", payload(repo, "t2"), now=5000)
    [outside] = observations(store, "outside")
    assert (outside["started_at"], outside["ended_at"]) == (1100, 5000)
    assert [c["kind"] for c in changes_of(store, outside["id"])] == [model.UPDATE]
    assert changes_of(store, observations(store, "agent")[0]["id"]) == []  # t1 changed nothing


def test_a_call_without_post_is_closed_at_stop(repo, store):
    handle(store, "claude-code", "pre", payload(repo, "t1", "sleep 100 && echo x >> shop.py"), now=1000)
    shell(repo, "echo partial >> shop.py")  # the call wrote something before it was interrupted
    handle(store, "claude-code", "stop", {"session_id": "s1", "cwd": str(repo)}, now=3000)
    [call] = observations(store, "agent")
    assert call["ended_at"] == 3000
    assert [c["kind"] for c in changes_of(store, call["id"])] == [model.UPDATE]
    assert store.open_observations(str(repo.resolve())) == []


def test_overlapping_calls_are_concurrent(repo, store):
    handle(store, "claude-code", "pre", payload(repo, "a"), now=1000)
    handle(store, "codex", "pre", payload(repo, "b", session="codex-thread"), now=1100)
    shell(repo, "echo x >> shop.py")
    handle(store, "codex", "post", payload(repo, "b", session="codex-thread"), now=1200)
    handle(store, "claude-code", "post", payload(repo, "a"), now=1300)
    calls = observations(store, "agent")
    assert [(c["agent"], c["concurrent"]) for c in calls] == [("claude-code", 1), ("codex", 1)]


def test_post_without_pre_still_records(repo, store):
    handle(store, "claude-code", "pre", payload(repo, "warmup"), now=500)  # baseline
    handle(store, "claude-code", "post", payload(repo, "warmup"), now=600)
    shell(repo, "echo y >> shop.py")
    handle(store, "claude-code", "post", payload(repo, "t9", "echo y >> shop.py"), now=1000)
    call = observations(store, "agent")[-1]
    assert (call["tool_call_id"], call["started_at"], call["ended_at"]) == ("t9", 1000, 1000)
    assert len(changes_of(store, call["id"])) == 1


def test_run_never_fails_and_never_prints(tmp_path, repo, capsys):
    db = tmp_path / "db" / "h.db"
    assert run("claude-code", "pre", "not json", db) == 0
    assert run("nope", "pre", "{}", db) == 0
    assert run("claude-code", "post", json.dumps(payload(repo, "t1")), db) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    log = (db.parent / "hooks.log").read_text()
    assert "claude-code pre failed" in log and "unknown hook nope pre" in log


# Payload shapes recorded from real hook calls (values replaced).
REAL_CLAUDE = {"session_id": "72a77881-0000-4000-8000-000000000000", "transcript_path": "/home/user/t.jsonl",
               "cwd": "/workspace", "permission_mode": "default", "hook_event_name": "PostToolUse",
               "tool_name": "Bash", "tool_input": {"command": "cat >> shop.py <<'EOF'\n…\nEOF", "description": "x"},
               "tool_response": {"stdout": "", "stderr": "", "interrupted": False}, "tool_use_id": "toolu_01Hm",
               "prompt_id": "p", "effort": "high", "scratchpad_dir": "/tmp/x", "duration_ms": 12}
REAL_CODEX = {"session_id": "01a10714-203f-7172-a0a4-09521747b2ad", "transcript_path": "/home/user/r.jsonl",
              "cwd": "/workspace", "permission_mode": "default", "hook_event_name": "PostToolUse", "model": "m",
              "tool_name": "Bash", "tool_input": {"command": "printf '…' >> shop.py"}, "tool_response": "ok",
              "tool_use_id": "exec-e3fcf1dd-3f97-4733-8df9-dc6ced498479", "turn_id": "01a10714-2ea5"}


def test_real_payloads_parse():
    from agent_history.hooks import PARSERS
    claude = PARSERS["claude-code"](REAL_CLAUDE)
    assert (claude.session_id, claude.tool_call_id, claude.tool_name, claude.cwd) == (
        "72a77881-0000-4000-8000-000000000000", "toolu_01Hm", "Bash", "/workspace")
    assert claude.command.startswith("cat >> shop.py")
    codex = PARSERS["codex"](REAL_CODEX)
    assert (codex.session_id, codex.tool_call_id, codex.tool_name) == (
        "01a10714-203f-7172-a0a4-09521747b2ad", "exec-e3fcf1dd-3f97-4733-8df9-dc6ced498479", "Bash")


def test_codex_hook_calls_link_to_codex_log_events(store):
    """Codex's hook `tool_use_id` is the item id in its session log; `session_id` is the thread id."""
    from agent_history.adapters import codex_rollout
    log = next((Path(__file__).parent / "fixtures" / "codex_rollout").glob("rollout-2026-10-04*.jsonl"))
    store.ingest(codex_rollout.normalize(log))
    event = store.event(store.linked_event_id(REAL_CODEX["session_id"], REAL_CODEX["tool_use_id"]))
    assert event.kind == model.COMMAND and event.payload["command"] == "rg --files"
