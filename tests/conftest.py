import time

import pytest


@pytest.fixture(autouse=True)
def no_real_agent_logs(tmp_path_factory, monkeypatch):
    """`ah log`/`ah blame` sync agent logs from this machine; tests must never see the real ones."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """`ah` prints local times; pin the zone so expected output doesn't depend on the machine."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
