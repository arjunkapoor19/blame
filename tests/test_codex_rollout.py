import json
from pathlib import Path

from agent_history import model
from agent_history.adapters import codex, codex_rollout
from agent_history.blame import blame, story
from agent_history.commands import is_test, summarize
from agent_history.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
# The shop.py session, recorded twice: by our recorder and by Codex's own session log.
SHOP_LOG = FIXTURES / "codex_rollout" / "rollout-2026-10-04T18-52-01-01a10714-203f-7172-a0a4-09521747b2ad.jsonl"
SHOP_CAPTURE = FIXTURES / "codex" / "20261004T132159Z-39de3d"
INTERRUPTED_LOG = FIXTURES / "codex_rollout" / "rollout-2026-09-29T22-57-38-01a0ee35-32d4-7953-aa95-4b6c59e16bf4.jsonl"


def test_shop_log_structure():
    n = codex_rollout.normalize(SHOP_LOG)
    assert n.session.id == "01a10714-203f-7172-a0a4-09521747b2ad"
    assert (n.session.agent, n.session.agent_version, n.session.cwd) == ("codex", "0.155.1", "/workspace")
    assert n.session.outcome == "completed"
    assert [(t.seq, t.status) for t in n.turns] == [(1, "completed"), (2, "completed")]
    assert n.turns[0].input_text.startswith("apply_discount in shop.py is buggy")

    edits = [e for e in n.events if e.kind == model.FILE_CHANGE]
    assert [(c.path, c.kind) for e in edits for c in e.file_changes] == [
        ("/workspace/test_shop.py", "add"), ("/workspace/test_shop.py", "update"), ("/workspace/shop.py", "update"),
        ("/workspace/test_shop.py", "update"), ("/workspace/shop.py", "update")]
    assert "+    return price * (1 - percent / 100)" in edits[2].file_changes[0].diff

    commands = [e for e in n.events if e.kind == model.COMMAND]
    tests = [c for c in commands if is_test(c.payload["command"])]
    assert [c.payload["exit_code"] for c in tests] == [127, 1, 1, 0, 1, 0]
    assert summarize(tests[0].payload, tests[0].status) == "command not found"
    assert all(c.payload["cwd"] == "/workspace" for c in commands)
    assert [a["type"] for a in commands[1].payload["actions"]] == ["read", "list"]
    assert commands[2].started_at < commands[2].ended_at or commands[2].payload["duration_ms"] == 0


def test_records_the_adapter_does_not_read_are_ignored():
    n = codex_rollout.normalize(SHOP_LOG)
    raw = [json.loads(line) for line in SHOP_LOG.open()]
    for event in n.events:
        for line in event.source_lines:
            assert raw[line - 1]["type"] == "event_msg"


def test_interrupted_log():
    n = codex_rollout.normalize(INTERRUPTED_LOG)
    assert [t.status for t in n.turns] == ["interrupted"]
    [command] = [e for e in n.events if e.kind == model.COMMAND]
    # Codex logs the killed command as failed with exit code -1; it was interrupted.
    assert command.status == "interrupted"
    assert summarize(command.payload, command.status) == "interrupted"


def test_unknown_items_are_kept(tmp_path):
    item = {"type": "SomethingNew", "id": "x1", "detail": 42}
    records = [
        {"timestamp": "2026-10-04T13:22:05.000Z", "type": "session_meta", "payload": {"id": "t1", "cwd": "/w"}},
        {"timestamp": "2026-10-04T13:22:06.000Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "u1"}},
        {"timestamp": "2026-10-04T13:22:07.000Z", "type": "event_msg",
         "payload": {"type": "item_completed", "thread_id": "t1", "turn_id": "u1", "item": item}},
    ]
    path = tmp_path / "rollout-x.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    n = codex_rollout.normalize(path)
    [event] = n.events
    assert event.kind == model.TOOL_CALL and event.payload["raw"] == item
    assert n.turns[0].status == "incomplete"  # the log ends mid-turn


def _comparable_story(store, origin):
    steps = []
    for e in story(store, origin).steps:
        if e.kind == model.COMMAND:
            steps.append((e.kind, e.payload["command"], summarize(e.payload, e.status)))
        elif e.kind == model.FILE_CHANGE:
            steps.append((e.kind, tuple((c.path, c.kind) for c in e.file_changes)))
        else:
            steps.append((e.kind, e.payload.get("text")))
    return steps


def test_log_and_capture_give_the_same_blame(tmp_path):
    """The same session through two different adapters must blame identically."""
    from_capture, from_log = Store(tmp_path / "capture.db"), Store(tmp_path / "log.db")
    from_capture.ingest(codex.normalize(SHOP_CAPTURE))
    from_log.ingest(codex_rollout.normalize(SHOP_LOG))

    def summary(store):
        lines = blame(store, "/workspace/shop.py").lines
        return [(l.number, l.text, store.turn(l.origin.turn_id)["seq"] if l.origin else None,
                 [t for _, t in l.history]) for l in lines]

    assert summary(from_capture) == summary(from_log)
    capture_line = blame(from_capture, "/workspace/shop.py").lines[5]
    log_line = blame(from_log, "/workspace/shop.py").lines[5]
    assert _comparable_story(from_capture, capture_line.origin) == _comparable_story(from_log, log_line.origin)
