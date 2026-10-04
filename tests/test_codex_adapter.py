import json
from collections import Counter
from pathlib import Path

from agent_history import model
from agent_history.adapters.codex import _unwrap_shell, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
BREAK_FIX = FIXTURES / "20260929T172400Z-cb44c2"  # 2 turns: create calc.py, then break and fix it
APPROVAL = FIXTURES / "20260929T172447Z-9d7320"  # one command, approval declined
INTERRUPTED = FIXTURES / "20260929T172738Z-ceaebe"  # turn interrupted mid-command


def kinds(session):
    return Counter(e.kind for e in session.events)


def test_break_fix_session_structure():
    n = normalize(BREAK_FIX)
    assert n.session.id == BREAK_FIX.name
    assert (n.session.agent, n.session.agent_version, n.session.cwd) == ("codex", "0.155.1", "/workspace")
    assert len(n.threads) == 1 and n.threads[0].parent_thread_id is None
    assert [(t.seq, t.status) for t in n.turns] == [(1, "completed"), (2, "completed")]
    assert n.turns[1].input_text.startswith("Now deliberately break add")

    counts = kinds(n)
    assert counts[model.USER_MESSAGE] == 2
    assert counts[model.COMMAND] == 4
    assert counts[model.FILE_CHANGE] == 3
    assert set(counts) <= set(model.KINDS)

    thread_id, turn_ids = n.threads[0].id, {t.id for t in n.turns}
    assert all(e.thread_id == thread_id and e.turn_id in turn_ids for e in n.events)
    assert [e.seq for e in n.events] == list(range(1, len(n.events) + 1))


def test_streaming_deltas_and_plumbing_are_dropped():
    raw = [json.loads(line) for line in (BREAK_FIX / "events.jsonl").open()]
    deltas = sum(1 for m in raw if str(m.get("method", "")).endswith("Delta") or
                 str(m.get("method", "")).endswith("delta"))
    assert deltas > 100
    n = normalize(BREAK_FIX)
    referenced = {line for e in n.events for line in e.source_lines}
    for line in referenced:
        method = raw[line - 1].get("method", "")
        assert not method.lower().endswith("delta")
        assert method not in {"account/rateLimits/updated", "mcpServer/startupStatus/updated"}


def test_source_lines_point_at_the_raw_records():
    raw = (BREAK_FIX / "events.jsonl").read_text().splitlines()
    n = normalize(BREAK_FIX)
    for event in n.events:
        source_id = event.payload.get("source_id")
        if source_id:
            for line in event.source_lines:
                assert json.loads(raw[line - 1])["params"]["item"]["id"] == source_id


def test_commands_are_unwrapped_and_failures_kept():
    n = normalize(BREAK_FIX)
    commands = [e for e in n.events if e.kind == model.COMMAND]
    assert commands[0].payload["command"] == "cat README.md && rg --files"
    assert [(c.status, c.payload["exit_code"]) for c in commands[1:]] == [
        ("completed", 0), ("failed", 1), ("completed", 0)]
    assert [a["type"] for a in commands[0].payload["actions"]] == ["read", "list"]
    assert [a["type"] for a in commands[1].payload["actions"]] == ["other"]


def test_file_changes():
    n = normalize(BREAK_FIX)
    edits = [e for e in n.events if e.kind == model.FILE_CHANGE]
    first = edits[0].file_changes
    assert [(c.path, c.kind) for c in first] == [("/workspace/calc.py", "add"), ("/workspace/test_calc.py", "add")]
    assert first[0].diff == "def add(a, b):\n    return a + b\n"
    assert edits[1].file_changes[0].kind == "update"
    assert "+    return a - b" in edits[1].file_changes[0].diff


def test_approval_links_to_the_command_it_gates():
    n = normalize(APPROVAL)
    command = next(e for e in n.events if e.kind == model.COMMAND)
    approval = next(e for e in n.events if e.kind == model.APPROVAL)
    assert approval.parent_id == command.id
    assert approval.status == "declined" and approval.payload["decision"] == "decline"
    assert approval.payload["command"].startswith("printf 'world")
    assert command.status == "declined" and command.payload["exit_code"] is None


def test_interrupted_turn_closes_dangling_items():
    n = normalize(INTERRUPTED)
    assert n.session.outcome == "interrupted"
    assert n.turns[0].status == "interrupted"
    command = next(e for e in n.events if e.kind == model.COMMAND)
    assert command.status == "interrupted"
    assert command.ended_at == n.turns[0].ended_at


def test_unknown_items_are_kept_not_dropped(tmp_path):
    item = {"type": "somethingNew", "id": "x1", "detail": 42}
    lines = [
        {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "u"}}, "emittedAtMs": 1},
        {"method": "item/completed", "params": {"item": item, "threadId": "t", "turnId": "u",
                                                "completedAtMs": 2}},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(m) for m in lines) + "\n")
    n = normalize(tmp_path)
    [event] = n.events
    assert event.kind == model.TOOL_CALL
    assert event.payload["raw"] == item


def test_subagent_threads_form_a_tree(tmp_path):
    item = {"type": "collabAgentToolCall", "id": "c1", "tool": "spawnAgent", "prompt": "write tests",
            "senderThreadId": "root", "receiverThreadIds": ["child"], "status": "completed"}
    lines = [
        {"method": "thread/started", "params": {"thread": {"id": "root"}}, "emittedAtMs": 1},
        {"method": "turn/started", "params": {"threadId": "root", "turn": {"id": "u"}}, "emittedAtMs": 1},
        {"method": "item/completed", "params": {"item": item, "threadId": "root", "turnId": "u"}},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(m) for m in lines) + "\n")
    n = normalize(tmp_path)
    assert {t.id: t.parent_thread_id for t in n.threads} == {"root": None, "child": "root"}
    assert n.events[0].kind == model.SUBAGENT_CALL
    assert n.events[0].payload["receiver_thread_ids"] == ["child"]


def test_unwrap_shell():
    assert _unwrap_shell("/bin/zsh -lc 'python3 -m unittest -q'") == "python3 -m unittest -q"
    assert _unwrap_shell("bash -c 'ls'") == "ls"
    assert _unwrap_shell("ls -la") == "ls -la"
    assert _unwrap_shell("/bin/zsh -lc 'unterminated") == "/bin/zsh -lc 'unterminated"
