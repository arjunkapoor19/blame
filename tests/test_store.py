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
    assert "✓⚠  2      return a + b" in out
    assert "2 of 2 lines written by a recorded agent: 2 ✓ tests passed after" in out

    assert main(["--db", db, "blame", "/workspace/calc.py:2"]) == 0
    out = capsys.readouterr().out
    assert "✓ tests passed after this change" in out
    assert "turn 2" in out
    assert "▶ 17:24:24  edit     ~calc.py   ← wrote this line" in out
    assert "test     ✗ python3 -m unittest -q  → 1 test, 1 failed" in out
    assert "rewritten 2 times" in out

    assert main(["--db", db, "blame", "/workspace/missing.py"]) == 1
