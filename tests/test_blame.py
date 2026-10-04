from pathlib import Path

import pytest

from agent_history import model
from agent_history.adapters.codex import normalize
from agent_history.blame import FAILED, PASSED, UNTESTED, Line, apply_hunks, blame, replay, story
from agent_history.model import Event, FileChange, NormalizedSession, Session, Thread, Turn
from agent_history.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
BREAK_FIX = FIXTURES / "20260929T172400Z-cb44c2"  # create calc.py, then break and fix it
SHOP = FIXTURES / "20261004T132159Z-39de3d"  # weak test noticed, strengthened, then the bug fixed


def texts(lines):
    return [line.text for line in lines]


def origins(lines):
    return [line.origin for line in lines]


def test_add_attributes_every_line():
    files = replay([("/f", "e1", "add", "a\nb\n", None)])
    assert texts(files["/f"]) == ["a", "b"]
    assert origins(files["/f"]) == ["e1", "e1"]


def test_update_replaces_and_keeps_history():
    files = replay([
        ("/f", "e1", "add", "a\nb\nc\n", None),
        ("/f", "e2", "update", "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n", None),
    ])
    assert texts(files["/f"]) == ["a", "B", "c"]
    assert origins(files["/f"]) == ["e1", "e2", "e1"]
    assert files["/f"][1].history == [("e1", "b"), ("e2", "B")]


def test_multi_hunk_update_shifts_later_hunks():
    lines = [Line(str(i), "e1") for i in range(1, 11)]
    # Insert two lines after 2, then replace line 8 (which is line 10 in the new file).
    apply_hunks(lines, "@@ -2,0 +3,2 @@\n+x\n+y\n@@ -8,1 +10,1 @@\n-8\n+EIGHT\n", "e2")
    assert texts(lines) == ["1", "2", "x", "y", "3", "4", "5", "6", "7", "EIGHT", "9", "10"]
    assert origins(lines)[2:4] == ["e2", "e2"] and origins(lines)[9] == "e2"


def test_pure_deletion():
    lines = [Line(t, "e1") for t in "abcd"]
    apply_hunks(lines, "@@ -2,2 +1,0 @@\n-b\n-c\n", "e2")
    assert texts(lines) == ["a", "d"]


def test_update_to_unrecorded_file_leaves_untouched_lines_unattributed():
    files = replay([("/f", "e1", "update", "@@ -3,2 +3,2 @@\n ctx\n-old\n+new\n", None)])
    lines = files["/f"]
    assert texts(lines) == [None, None, "ctx", "new"]
    assert origins(lines) == [None, None, None, "e1"]


def test_delete_and_move():
    files = replay([
        ("/a", "e1", "add", "x\n", None),
        ("/b", "e1", "add", "y\n", None),
        ("/a", "e2", "delete", None, None),
        ("/b", "e3", "move", "@@ -1 +1 @@\n-y\n+z\n", "/c"),
    ])
    assert set(files) == {"/c"}
    assert texts(files["/c"]) == ["z"] and origins(files["/c"]) == ["e3"]


def test_hunk_without_line_numbers_is_reported(tmp_path):
    warnings = []
    replay([("/f", "e1", "update", "@@\n-a\n+b\n", None)], warnings)
    assert warnings and "e1" in warnings[0]


def test_blame_break_fix_fixture(tmp_path):
    store = Store(tmp_path / "h.db")
    store.ingest(normalize(BREAK_FIX))
    result = blame(store, "/workspace/calc.py")
    assert not result.on_disk
    assert [l.text for l in result.lines] == ["def add(a, b):", "    return a + b"]

    line = result.lines[1]
    assert line.origin.kind == model.FILE_CHANGE
    turn = store.turn(line.origin.turn_id)
    assert turn["seq"] == 2
    assert [text for _, text in line.history] == ["    return a + b", "    return a - b", "    return a + b"]

    assert line.rewrites == 2
    assert line.verification.status == PASSED

    with pytest.raises(LookupError):
        blame(store, "/workspace/nope.py")


def _session_writing(path: Path, content: str) -> NormalizedSession:
    session = Session("s1", "test-agent", None, str(path.parent), 0, 1, "completed", "test")
    event = Event("s1:1", "s1", "t", "u", 1, model.FILE_CHANGE, "completed", 0, 1,
                  file_changes=[FileChange(str(path), model.ADD, content)])
    return NormalizedSession(session, [Thread("t", "s1")], [Turn("u", "t", 1)], [event])


def test_blame_aligns_with_disk_after_human_edits(tmp_path):
    target = tmp_path / "f.py"
    store = Store(tmp_path / "h.db")
    store.ingest(_session_writing(target, "one\ntwo\nthree\n"))
    # A human inserts a line and edits another after the agent finished.
    target.write_text("zero\none\nTWO\nthree\n")
    result = blame(store, str(target))
    assert result.on_disk
    assert [(l.text, l.origin.id if l.origin else None) for l in result.lines] == [
        ("zero", None), ("one", "s1:1"), ("TWO", None), ("three", "s1:1")]


def test_declined_file_changes_are_not_blamed(tmp_path):
    target = tmp_path / "f.py"
    normalized = _session_writing(target, "x\n")
    normalized.events[0].status = "declined"
    store = Store(tmp_path / "h.db")
    store.ingest(normalized)
    with pytest.raises(LookupError):
        blame(store, str(target))


def test_story_tells_the_whole_turn(tmp_path):
    store = Store(tmp_path / "h.db")
    store.ingest(normalize(SHOP))
    line = blame(store, "/workspace/shop.py").lines[5]
    assert line.text == "    return price * (1 - percent / 100)"

    told = story(store, line.origin)
    assert told.prompt.startswith("apply_discount in shop.py is buggy")
    assert told.hidden_before == 0
    steps = [(e.kind, e.status) for e in told.steps]
    origin_at = told.steps.index(line.origin)
    # Before the fix: the test file is written, tests fail, the weak test is strengthened, tests fail again.
    before = told.steps[:origin_at]
    assert [c.path.rsplit("/", 1)[-1] for e in before if e.kind == model.FILE_CHANGE
            for c in e.file_changes] == ["test_shop.py", "test_shop.py"]
    assert any("accidentally passed" in e.payload.get("text", "") for e in before)
    assert [e.payload["exit_code"] for e in before if e.kind == model.COMMAND][-3:] == [127, 1, 1]
    # After: the passing test run, then the final answer, and nothing from turn 2.
    after = told.steps[origin_at + 1:]
    assert [(e.kind, e.status) for e in after] == [(model.COMMAND, "completed"), (model.AGENT_MESSAGE, "completed")]
    assert after[-1].payload["phase"] == "final"
    assert all(e.turn_id == line.origin.turn_id for e in told.steps)
    assert (model.TOKEN_USAGE, None) not in steps


def test_verification_on_real_session(tmp_path):
    store = Store(tmp_path / "h.db")
    store.ingest(normalize(SHOP))
    result = blame(store, "/workspace/shop.py")
    written = {l.number: l.verification for l in result.lines if l.origin}
    assert sorted(written) == [6, 10, 11]
    assert all(v.status == PASSED for v in written.values())
    # The deciding run is the session's last test run, in turn 2.
    assert written[6].check.payload["output"].rstrip().endswith("OK")
    assert store.turn(written[6].check.turn_id)["seq"] == 2


def _session(path: Path, commands: list[tuple[str, int]]) -> NormalizedSession:
    """One turn: write `path`, then run each (command, exit code)."""
    normalized = _session_writing(path, "x = 1\n")
    for seq, (command, exit_code) in enumerate(commands, start=2):
        normalized.events.append(Event(
            f"s1:{seq}", "s1", "t", "u", seq, model.COMMAND, "completed" if exit_code == 0 else "failed",
            seq, seq, payload={"command": command, "exit_code": exit_code, "output": ""}))
    return normalized


@pytest.mark.parametrize("commands, status", [
    ([], UNTESTED),
    ([("ls", 0), ("python3 build.py", 1)], UNTESTED),  # not test runs
    ([("pytest -q", 127)], UNTESTED),  # the runner never ran
    ([("pytest -q", 0)], PASSED),
    ([("pytest -q", 0), ("pytest -q", 1)], FAILED),  # the latest run decides
    ([("pytest -q", 1), ("cd app && npm test", 0)], PASSED),
])
def test_verification_statuses(tmp_path, commands, status):
    store = Store(tmp_path / "h.db")
    store.ingest(_session(tmp_path / "f.py", commands))
    [line] = blame(store, str(tmp_path / "f.py")).lines
    assert line.verification.status == status
