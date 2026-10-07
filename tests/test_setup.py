import json

import pytest

from agent_history import setup as agent_setup
from agent_history.cli import main

THEIRS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "their-linter"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps(THEIRS, indent=2))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return claude / "settings.json", codex / "hooks.json"


def commands(settings, event):
    return [h["command"] for entry in settings["hooks"].get(event, []) for h in entry["hooks"]]


def test_install_keeps_everything_else(homes):
    claude, _ = homes
    agent_setup.install(agent_setup.AGENTS["claude-code"], "/usr/local/bin/ah")
    settings = json.loads(claude.read_text())
    assert settings["model"] == "opus" and settings["permissions"] == THEIRS["permissions"]
    assert commands(settings, "PreToolUse") == ["their-linter", "/usr/local/bin/ah hook claude-code pre # agent-history"]
    assert commands(settings, "PostToolUse") == ["/usr/local/bin/ah hook claude-code post # agent-history"]
    assert commands(settings, "Stop") == commands(settings, "UserPromptSubmit") == \
        ["/usr/local/bin/ah hook claude-code stop # agent-history"]
    assert commands(settings, "Notification") == ["say done"]
    ours = settings["hooks"]["PreToolUse"][1]
    assert ours["matcher"] == "Bash|Edit|MultiEdit|Write|NotebookEdit"
    assert json.loads(claude.with_name("settings.json.agent-history.bak").read_text()) == THEIRS


def test_install_is_idempotent_and_remove_restores(homes):
    claude, _ = homes
    agent = agent_setup.AGENTS["claude-code"]
    agent_setup.install(agent, "/old/ah")
    agent_setup.install(agent, "/new/ah")  # re-running replaces, never duplicates
    settings = json.loads(claude.read_text())
    assert commands(settings, "PreToolUse") == ["their-linter", "/new/ah hook claude-code pre # agent-history"]
    assert agent_setup.remove(agent)
    assert json.loads(claude.read_text()) == THEIRS
    assert not agent_setup.remove(agent)


def test_codex_hooks_file_is_created_and_removed(homes):
    _, codex = homes
    agent = agent_setup.AGENTS["codex"]
    agent_setup.install(agent, "/bin/ah", db="/data/my history.db")
    hooks = json.loads(codex.read_text())
    assert commands(hooks, "PostToolUse") == ["/bin/ah --db '/data/my history.db' hook codex post # agent-history"]
    agent_setup.remove(agent)
    assert json.loads(codex.read_text()) == {}


def test_cli(homes, capsys):
    claude, codex = homes
    assert main(["setup", "claude-code"]) == 0
    out = capsys.readouterr().out
    assert "claude-code: added hooks to" in out and "PostToolUse [Bash|Edit|MultiEdit|Write|NotebookEdit]" in out
    assert not codex.exists()
    assert main(["setup", "--remove"]) == 0
    assert json.loads(claude.read_text()) == THEIRS
    assert main(["setup", "cursor"]) == 1
