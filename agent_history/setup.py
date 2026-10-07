"""`ah setup [--remove]`: install (or remove) the hooks that let `ah` observe agents' tool calls.

Hooks go into each agent's user-level settings, so every project is covered. Entries are
tagged with a trailing `# agent-history` shell comment, which is how they're found again:
`--remove` deletes exactly those and nothing else. Settings are backed up before changing.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

TAG = "# agent-history"


@dataclass(frozen=True)
class Agent:
    name: str
    settings: Callable[[], Path]  # the file holding its hooks
    matcher: str  # tools whose calls can change files
    stop_events: tuple[str, ...]  # events after which no open tool call will finish
    note: str | None = None  # a step the user must take after installing


def _claude_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def _codex_hooks() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


AGENTS = {
    "claude-code": Agent("claude-code", _claude_settings, "Bash|Edit|MultiEdit|Write|NotebookEdit",
                         ("Stop", "UserPromptSubmit")),
    "codex": Agent("codex", _codex_hooks, "*", ("Stop", "UserPromptSubmit"),
                   note="Codex asks you to review new hooks once: open `codex` and trust them when prompted."),
}


def hook_command(ah: str, agent: str, event: str, db: str | None = None) -> str:
    db_flag = f" --db {_quote(db)}" if db else ""
    return f"{_quote(ah)}{db_flag} hook {agent} {event} {TAG}"


def install(agent: Agent, ah: str, db: str | None = None) -> list[str]:
    """Add `ah`'s hooks to the agent's settings (replacing any earlier ones). Returns what changed."""
    path = agent.settings()
    settings = _read(path)
    hooks = _without_ours(settings.get("hooks") or {})
    added = []
    for event, phase, matcher in [("PreToolUse", "pre", agent.matcher), ("PostToolUse", "post", agent.matcher)] + \
            [(stop, "stop", None) for stop in agent.stop_events]:
        entry: dict[str, Any] = {"hooks": [{"type": "command", "command": hook_command(ah, agent.name, phase, db)}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        hooks.setdefault(event, []).append(entry)
        added.append(f"{event}{f' [{matcher}]' if matcher else ''} → ah hook {agent.name} {phase}")
    settings["hooks"] = hooks
    _write(path, settings)
    return added


def remove(agent: Agent) -> bool:
    """Remove `ah`'s hooks from the agent's settings. Returns whether anything was removed."""
    path = agent.settings()
    if not path.exists():
        return False
    settings = _read(path)
    before = settings.get("hooks") or {}
    after = _without_ours(before)
    if after == before:
        return False
    if after:
        settings["hooks"] = after
    else:
        settings.pop("hooks", None)
    _write(path, settings)
    return True


def ah_executable() -> str:
    """The `ah` to put in hooks: the one on PATH if there is one, else the one running now."""
    return shutil.which("ah") or str(Path(sys.argv[0]).resolve())


def _without_ours(hooks: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for event, entries in hooks.items():
        kept = []
        for entry in entries if isinstance(entries, list) else []:
            inner = [h for h in entry.get("hooks") or [] if TAG not in str(h.get("command", ""))]
            if inner:
                kept.append({**entry, "hooks": inner})
            elif not entry.get("hooks"):
                kept.append(entry)
        if kept:
            cleaned[event] = kept
    return cleaned


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} doesn't hold a JSON object")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".agent-history.bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _quote(value: str) -> str:
    return value if all(c.isalnum() or c in "/._-~" for c in value) else "'" + value.replace("'", "'\\''") + "'"
