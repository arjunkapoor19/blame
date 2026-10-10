import json
import shutil
import subprocess

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


def test_opencode_plugin_is_written_and_removed():
    agent = agent_setup.AGENTS["opencode"]
    plugin = agent.settings()
    assert plugin.parts[-3:] == ("opencode", "plugin", "agent-history.js")
    agent_setup.install(agent, "/old/ah")
    agent_setup.install(agent, "/bin/ah", db="/data/my history.db")  # re-running replaces
    text = plugin.read_text()
    assert text.startswith("// agent-history") and 'const AH = ["/bin/ah", "--db", "/data/my history.db"]' in text
    assert agent_setup.remove(agent) and not plugin.exists()
    assert not agent_setup.remove(agent)


def test_opencode_plugin_never_replaces_someone_elses_file():
    agent = agent_setup.AGENTS["opencode"]
    plugin = agent.settings()
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export const Mine = async () => ({})\n")
    with pytest.raises(ValueError):
        agent_setup.install(agent, "/bin/ah")
    assert not agent_setup.remove(agent)
    assert plugin.read_text() == "export const Mine = async () => ({})\n"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the plugin")
def test_opencode_plugin_runs_ah_around_tool_calls(tmp_path):
    """Load the generated plugin and call its hooks the way opencode does."""
    calls = tmp_path / "calls.jsonl"
    fake_ah = tmp_path / "ah"
    fake_ah.write_text(f'#!/bin/sh\nprintf \'{{"args": "%s", "stdin": %s}}\\n\' "$*" "$(cat)" >> {calls}\n')
    fake_ah.chmod(0o755)
    agent = agent_setup.AGENTS["opencode"]
    agent_setup.install(agent, str(fake_ah))
    script = tmp_path / "run.mjs"
    script.write_text(f"""
        import {{ AgentHistory }} from {json.dumps(str(agent.settings()))}
        const hooks = await AgentHistory({{ directory: "/workspace" }})
        const input = {{ tool: "bash", sessionID: "ses_1", callID: "call_1", args: {{ command: "sed -i x a.py" }} }}
        await hooks["tool.execute.before"](input, {{ args: input.args }})
        await hooks["tool.execute.after"](input, {{ title: "", output: "", metadata: {{}} }})
        await hooks.event({{ event: {{ type: "message.updated", properties: {{}} }} }})
        await hooks.event({{ event: {{ type: "session.idle", properties: {{ sessionID: "ses_1" }} }} }})
    """)
    subprocess.run(["node", str(script)], check=True, timeout=30)
    seen = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [c["args"] for c in seen] == ["hook opencode pre", "hook opencode post", "hook opencode stop"]
    assert seen[0]["stdin"] == {"cwd": "/workspace", "session_id": "ses_1", "tool_use_id": "call_1",
                                "tool_name": "bash", "tool_input": {"command": "sed -i x a.py"}}
    assert seen[2]["stdin"] == {"cwd": "/workspace", "session_id": "ses_1"}
