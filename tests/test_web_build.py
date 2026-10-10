"""The viewer's built page ships in the package (agent_history/static/), so users never need Node.
These tests catch a `web/` change that was never rebuilt, and a page that would reach the network."""

import hashlib
import re
from pathlib import Path

import pytest

from agent_history import view

WEB = Path(__file__).parent.parent / "web"


def source_hash(root: Path) -> str:
    """The same hash web/build.ts writes: every file under web/ except node_modules, dot-files
    and screenshots, sorted by path."""
    files = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*")
        if path.is_file() and not any(part.startswith(".") or part == "node_modules"
                                      for part in path.relative_to(root).parts))
    digest = hashlib.sha256()
    for name in files:
        if name.startswith("screenshots/"):
            continue
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()


@pytest.mark.skipif(not WEB.is_dir(), reason="no web/ sources (an installed package)")
def test_built_page_matches_its_sources():
    built = (view.STATIC / ".source-hash").read_text().strip()
    assert built == source_hash(WEB), "web/ changed since the last build: run `npm run build` in web/"


def test_built_page_needs_nothing_from_the_network():
    index = (view.STATIC / "index.html").read_text()
    assert re.findall(r'(?:src|href)="(https?:)?//', index) == []
    for asset in re.findall(r'(?:src|href)="/(assets/[^"]+)"', index):
        assert (view.STATIC / asset).is_file()
