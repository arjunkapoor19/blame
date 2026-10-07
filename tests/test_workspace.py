import os
import subprocess
from pathlib import Path

import pytest

from agent_history import model
from agent_history.store import Store
from agent_history.workspace import MAX_BLOB, RACY_NS, Blobs, list_files, root_of, snapshot, unified_diff


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db" / "h.db")


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text("ignored.txt\nnode_modules/\n")
    age(path / ".gitignore")
    return path


def age(path: Path, seconds: float = 10) -> None:
    """Backdate a file so its timestamp is trusted (older than the racy window)."""
    when = path.stat().st_mtime - seconds
    os.utime(path, (when, when))


def kinds(changes):
    return sorted((Path(c.path).name, c.kind) for c in changes)


def test_first_snapshot_is_a_baseline(tmp_path, store):
    repo = git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("one\n")
    (repo / "ignored.txt").write_text("x\n")
    baseline, changes = snapshot(store, repo)
    assert baseline
    assert kinds(changes) == [(".gitignore", model.ADD), ("a.py", model.ADD)]  # .gitignore'd files are skipped
    added = next(c for c in changes if c.path.endswith("a.py"))
    assert Blobs(store.objects_dir).text(added.after) == "one\n"


def test_changes_between_snapshots(tmp_path, store):
    repo = git_repo(tmp_path / "repo")
    for name in ("keep.py", "edit.py", "gone.py"):
        (repo / name).write_text(f"{name}\n")
        age(repo / name)
    snapshot(store, repo)
    (repo / "edit.py").write_text("edited\n")
    (repo / "gone.py").unlink()
    (repo / "new.py").write_text("new\n")
    baseline, changes = snapshot(store, repo)
    assert not baseline
    assert kinds(changes) == [("edit.py", model.UPDATE), ("gone.py", model.DELETE), ("new.py", model.ADD)]
    edit = next(c for c in changes if c.path.endswith("edit.py"))
    blobs = Blobs(store.objects_dir)
    assert (blobs.text(edit.before), blobs.text(edit.after)) == ("edit.py\n", "edited\n")
    assert snapshot(store, repo) == (False, [])  # nothing changed since


def test_only_changed_files_are_read(tmp_path, store, monkeypatch):
    repo = git_repo(tmp_path / "repo")
    for i in range(20):
        (repo / f"f{i}.py").write_text(f"{i}\n")
        age(repo / f"f{i}.py")
    snapshot(store, repo)
    (repo / "f3.py").write_text("changed\n")
    reads = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self.name) or original(self))
    _, changes = snapshot(store, repo)
    assert reads == ["f3.py"]
    assert kinds(changes) == [("f3.py", model.UPDATE)]


def test_same_size_rewrite_right_after_a_snapshot_is_caught(tmp_path, store):
    """Agents rewrite files within milliseconds; timestamps that close to a snapshot aren't trusted."""
    repo = git_repo(tmp_path / "repo")
    target = repo / "a.py"
    target.write_text("aaaa\n")
    snapshot(store, repo)
    stat = target.stat()
    target.write_text("bbbb\n")  # same size
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # and even the same timestamp
    _, changes = snapshot(store, repo)
    assert kinds(changes) == [("a.py", model.UPDATE)]
    assert RACY_NS > 0


def test_binary_and_large_files_are_tracked_without_content(tmp_path, store):
    repo = git_repo(tmp_path / "repo")
    (repo / "image.bin").write_bytes(b"\x89PNG\0\0data")
    (repo / "big.txt").write_text("x" * (MAX_BLOB + 1))
    _, changes = snapshot(store, repo)
    blobs = Blobs(store.objects_dir)
    by_name = {Path(c.path).name: c for c in changes}
    assert blobs.get(by_name["image.bin"].after) is None
    assert blobs.get(by_name["big.txt"].after) is None
    (repo / "image.bin").write_bytes(b"\x89PNG\0\0other")
    _, changes = snapshot(store, repo)
    assert kinds(changes) == [("image.bin", model.UPDATE)]  # still detected, by hash


def test_outside_git_common_directories_are_skipped(tmp_path):
    plain = tmp_path / "plain"
    (plain / "node_modules" / "pkg").mkdir(parents=True)
    (plain / "node_modules" / "pkg" / "index.js").write_text("x")
    (plain / "src").mkdir()
    (plain / "src" / "app.py").write_text("x")
    assert root_of(plain / "src") == (plain / "src").resolve()
    assert list_files(plain) == [os.path.join("src", "app.py")]


def test_root_of_a_repo_subdirectory(tmp_path):
    repo = git_repo(tmp_path / "repo")
    (repo / "pkg").mkdir()
    assert root_of(repo / "pkg") == repo.resolve()


def test_unified_diff_replays():
    from agent_history.blame import Line, apply_hunks
    before, after = "a\nb\nc\nd\n", "a\nB\nc\nd\ne\n"
    lines = [Line(t) for t in before.splitlines()]
    apply_hunks(lines, unified_diff(before, after), "e1")
    assert [l.text for l in lines] == after.splitlines()
    lines = [Line(t) for t in before.splitlines()]
    apply_hunks(lines, unified_diff(before, ""), "e2")
    assert lines == []
