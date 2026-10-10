from pathlib import Path

import pytest

from agent_history import model
from agent_history.adapters.codex import normalize
from agent_history.cli import main
from agent_history.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
BREAK_FIX = FIXTURES / "20260929T172400Z-cb44c2"


def test_ingest_round_trip(tmp_path):
    normalized = normalize(BREAK_FIX)
    store = Store(tmp_path / "h.db")
    store.ingest(normalized)
    events = store.events(session_id=normalized.session.id)
    assert [(e.id, e.kind, e.status, e.payload, e.source_lines) for e in events] == [
        (e.id, e.kind, e.status, e.payload, e.source_lines) for e in normalized.events]
    edit = next(e for e in events if e.kind == model.FILE_CHANGE)
    assert [(c.path, c.kind) for c in edit.file_changes] == [
        ("/workspace/calc.py", "add"), ("/workspace/test_calc.py", "add")]


def test_reingest_is_idempotent(tmp_path):
    store = Store(tmp_path / "h.db")
    store.ingest(normalize(BREAK_FIX))
    first = store.counts()
    store.ingest(normalize(BREAK_FIX))
    assert store.counts() == first
    assert first["sessions"] == 1 and first["file_changes"] == 4


def test_find_session_by_unique_part(tmp_path):
    store = Store(tmp_path / "h.db")
    for capture in FIXTURES.iterdir():
        store.ingest(normalize(capture))
    assert store.find_session("cb44c2")["id"] == BREAK_FIX.name
    assert store.find_session("nothing") is None
    with pytest.raises(LookupError):
        store.find_session("20260929")


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "h.db")
    assert main(["--db", db, "ingest", str(BREAK_FIX)]) == 0
    assert main(["--db", db, "log", "cb44c2"]) == 0
    log = capsys.readouterr().out
    assert "test     ✗ python3 -m unittest -q  → 1 test, 1 failed · AssertionError: -1 != 5" in log
    assert "read     README.md, list files" in log
    assert "edit     +calc.py, +test_calc.py" in log

    assert main(["--db", db, "blame", "/workspace/calc.py"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["author", "session", "turn", "when", "#", "code"]
    assert "codex    cb44c2  t2   Sep 29 '26 17:24  2      return a + b" in out

    assert main(["--db", db, "blame", "/workspace/calc.py:2"]) == 0
    out = capsys.readouterr().out
    assert "turn 2" in out
    assert "▶ 17:24:24  edit     ~calc.py   ← wrote this line" in out
    assert "test     ✗ python3 -m unittest -q  → 1 test, 1 failed" in out
    assert "rewritten 2 times" in out

    assert main(["--db", db, "blame", "/workspace/missing.py"]) == 1


def test_cli_blame_shows_what_the_agent_did(tmp_path, capsys):
    db = str(tmp_path / "h.db")
    assert main(["--db", db, "ingest", str(FIXTURES / "20261004T132159Z-39de3d")]) == 0
    capsys.readouterr()
    assert main(["--db", db, "blame", "/workspace/shop.py:6"]) == 0
    out = capsys.readouterr().out
    detail = " " * 21 + "│ "
    # The edit that wrote the line: full diff for shop.py, with the line marked.
    assert detail + "-    return price - percent\n" in out
    assert detail + "+    return price * (1 - percent / 100)   ← this line\n" in out
    # An earlier edit to the test file is shown, not just named.
    assert detail + "+        self.assertEqual(apply_discount(200, 20), 160)\n" in out
    # A new file is previewed, not dumped.
    assert detail + "… 6 more lines\n" in out
    # Agent messages are shown in full.
    assert "I’ll correct `apply_discount`." in out  # the end of a 230-character message
    assert "\033[" not in out  # no colour codes when not writing to a terminal
