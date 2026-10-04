import json
import shutil
from pathlib import Path

import pytest

from agent_history import model, sources
from agent_history.adapters import codex
from agent_history.blame import blame
from agent_history.cli import main
from agent_history.model import Event, FileChange, NormalizedSession, Session, Thread, Turn
from agent_history.sources import FAILED, INGESTED, SKIPPED, Source
from agent_history.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
SHOP_LOG = FIXTURES / "codex_rollout" / "rollout-2026-10-04T18-52-01-01a10714-203f-7172-a0a4-09521747b2ad.jsonl"
SHOP_CAPTURE = FIXTURES / "codex" / "20261004T132159Z-39de3d"


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    (home / "sessions" / "2026" / "10" / "04").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def install_log(codex_home: Path) -> Path:
    return Path(shutil.copy(SHOP_LOG, codex_home / "sessions" / "2026" / "10" / "04"))


def test_sync_picks_up_new_and_changed_logs_only(tmp_path, codex_home):
    store = Store(tmp_path / "h.db")
    assert sources.sync(store) == []
    log = install_log(codex_home)

    [result] = sources.sync(store)
    assert (result.status, result.source, result.session_id) == (INGESTED, "codex-log", SHOP_LOG.stem[-36:])
    assert sources.sync(store) == []  # unchanged

    with log.open("a") as f:  # the session continues
        f.write(json.dumps({"timestamp": "2026-10-04T13:30:00.000Z", "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "turn-3"}}) + "\n")
    [result] = sources.sync(store)
    assert result.status == INGESTED
    assert len(store.turns(result.session_id)) == 3
    assert store.counts()["sessions"] == 1


def test_capture_beats_log_for_the_same_session(tmp_path, codex_home):
    store = Store(tmp_path / "h.db")
    assert sources.ingest(store, SHOP_CAPTURE).status == INGESTED
    install_log(codex_home)
    [result] = sources.sync(store)
    assert result.status == SKIPPED and "codex-capture" in result.reason
    assert [s["id"] for s in store.sessions()] == [SHOP_CAPTURE.name]


def test_capture_replaces_an_earlier_log(tmp_path, codex_home):
    store = Store(tmp_path / "h.db")
    install_log(codex_home)
    sources.sync(store)
    assert sources.ingest(store, SHOP_CAPTURE).status == INGESTED
    assert [s["id"] for s in store.sessions()] == [SHOP_CAPTURE.name]
    assert store.counts()["threads"] == 1
    assert len(blame(store, "/workspace/shop.py").lines) == 12


def test_sessions_stored_before_sources_had_names(tmp_path, codex_home):
    store = Store(tmp_path / "h.db")
    store.ingest(codex.normalize(SHOP_CAPTURE))  # no source name, as in earlier databases
    install_log(codex_home)
    [result] = sources.sync(store)
    assert result.status == SKIPPED  # still recognised as a capture from its path


def test_explicit_ingest_rejects_unknown_paths(tmp_path):
    with pytest.raises(LookupError):
        sources.ingest(Store(tmp_path / "h.db"), tmp_path)


# A made-up agent with its own log format. Supporting it takes an adapter and a Source;
# nothing in the store, sync, blame or CLI changes.
def toy_normalize(path: Path) -> NormalizedSession:
    data = json.loads(path.read_text())
    if "boom" in data:
        raise ValueError("unreadable toy log")
    sid = data["session"]
    session = Session(sid, "toy-agent", "1.0", data["cwd"], 0, 1, "completed", str(path))
    events = [Event(f"{sid}:{i}", sid, sid, f"{sid}-turn", i, model.FILE_CHANGE, "completed", i, i,
                    file_changes=[FileChange(edit["path"], model.ADD, edit["content"])])
              for i, edit in enumerate(data["edits"], start=1)]
    return NormalizedSession(session, [Thread(sid, sid)], [Turn(f"{sid}-turn", sid, 1, data["prompt"])], events)


def toy_source(directory: Path) -> Source:
    return Source("toy-log", 1, discover=lambda: sorted(directory.glob("*.toy.json")),
                  matches=lambda p: p.name.endswith(".toy.json"), normalize=toy_normalize)


def test_any_agent_can_plug_in(tmp_path):
    logs = tmp_path / "toy"
    logs.mkdir()
    (logs / "a.toy.json").write_text(json.dumps({
        "session": "toy-1", "cwd": "/proj", "prompt": "write hello",
        "edits": [{"path": "/proj/hello.txt", "content": "hello\nworld\n"}]}))
    (logs / "b.toy.json").write_text(json.dumps({"boom": True}))
    store = Store(tmp_path / "h.db")

    results = {r.path.name: r for r in sources.sync(store, [toy_source(logs)])}
    assert results["a.toy.json"].status == INGESTED
    assert results["b.toy.json"].status == FAILED and "unreadable" in results["b.toy.json"].reason
    lines = blame(store, "/proj/hello.txt").lines
    assert [(l.text, l.origin.session_id) for l in lines] == [("hello", "toy-1"), ("world", "toy-1")]
    assert store.find_session("toy-1")["source_kind"] == "toy-log"


def test_cli_blame_syncs_automatically(tmp_path, codex_home, capsys):
    install_log(codex_home)
    db = str(tmp_path / "h.db")
    assert main(["--db", db, "blame", "/workspace/shop.py:6"]) == 0
    captured = capsys.readouterr()
    assert "synced 1 new or updated sessions (codex-log: 1)" in captured.err
    assert "▶" in captured.out and "edit     ~shop.py" in captured.out

    assert main(["--db", db, "log"]) == 0
    assert "synced" not in capsys.readouterr().err  # nothing new the second time


def test_cli_ingest_without_paths_syncs(tmp_path, codex_home, capsys):
    db = str(tmp_path / "h.db")
    assert main(["--db", db, "ingest"]) == 0
    assert "everything is up to date" in capsys.readouterr().out
    install_log(codex_home)
    assert main(["--db", db, "ingest"]) == 0
    assert "ingested 01a10714-203f-7172-a0a4-09521747b2ad  (codex-log)" in capsys.readouterr().out


def test_stored_sessions_are_renormalized_when_adapters_change(tmp_path, codex_home):
    store = Store(tmp_path / "h.db")
    assert sources.ingest(store, SHOP_CAPTURE).status == INGESTED
    install_log(codex_home)
    sources.sync(store)
    assert store.model_version() == model.VERSION
    # Simulate sessions normalized by an older adapter.
    store.db.execute("UPDATE events SET payload = '{}'")
    store.db.commit()
    store.db.execute("DELETE FROM meta")
    store.db.commit()

    results = sources.sync(store)
    assert [(r.source, r.status) for r in results] == [("codex-capture", INGESTED), ("codex-log", SKIPPED)]
    assert all(e.payload for e in store.events(session_id=SHOP_CAPTURE.name))
    assert store.model_version() == model.VERSION
    assert sources.sync(store) == []  # nothing left to do
