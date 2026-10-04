"""Understanding commands an agent ran: is it a test run, and what did it show?

Works only on the canonical command payload (`command`, `exit_code`, `output`,
`actions`), so it applies to every agent. Output is parsed from the end because
agents may truncate the start of long output.
"""

from __future__ import annotations

import os
import re
from typing import Any

# A test runner at the start of a command segment (`cd x && pytest`, `a; npm test`), optionally
# behind env vars, a path (`./gradlew`, `.venv/bin/pytest`) or a wrapper (`uv run`, `npx`).
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|()]")
TEST_RUNNER = re.compile(r"""
    ^\s*
    (?:\w+=\S*\s+)*
    (?:(?:uv|poetry|pipenv|hatch|pdm|rye)\s+run\s+|npx\s+|bunx\s+|pnpm\s+exec\s+|timeout\s+\S+\s+)*
    (?:\S*/)?
    (?: py\.?test
      | python[\d.]*\s+-m\s+(?:unittest|pytest)
      | tox | nox
      | (?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test
      | jest | vitest | mocha
      | go\s+test | cargo\s+(?:test|nextest) | deno\s+test
      | rspec | phpunit | dotnet\s+test | make\s+test
      | mvn\s+(?:\S+\s+)*test | gradlew?\s+(?:\S+\s+)*test
    )
    (?=\s|$)
""", re.VERBOSE)

UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ")
UNITTEST_FAILED = re.compile(r"^FAILED \((.*)\)$")
PYTEST_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected)(?:, )?)+)"
    r"(?:, \d+ warnings?)? in [\d.]+s")
JEST_SUMMARY = re.compile(r"^Tests:?\s+(\d.*)$")
ERROR_LINES = (
    re.compile(r"^FAILED (?!\()(.+)$"),  # pytest short summary: FAILED test_x.py::test_y - AssertionError
    re.compile(r"^(?:E\s+)?([\w.]*(?:Error|Exception)\b(?::.*)?)$"),  # Python exceptions, pytest `E` lines
    re.compile(r"^(error(?:\[\w+\])?:.+)$"),  # rustc, tsc and friends
)
READ_ONLY_ACTIONS = {"read", "listFiles", "search"}
LIMIT = 100


def is_test(command: str | None) -> bool:
    return bool(command) and any(TEST_RUNNER.match(segment) for segment in SEGMENT_SPLIT.split(command))


def verdict(payload: dict[str, Any], status: str | None) -> str | None:
    """'passed' or 'failed' for a test run that actually ran; None for anything else."""
    if not is_test(payload.get("command")) or status in ("declined", "interrupted", "incomplete"):
        return None
    exit_code = payload.get("exit_code")
    if exit_code is None or exit_code in (126, 127):
        return None
    return "passed" if exit_code == 0 else "failed"


def summarize(payload: dict[str, Any], status: str | None) -> str | None:
    """One line of evidence for what a command showed, or None if nothing is worth saying."""
    if status == "declined":
        return "declined"
    if status in ("interrupted", "incomplete"):
        return status
    exit_code = payload.get("exit_code")
    output = payload.get("output") or ""
    failed = exit_code not in (0, None)
    if exit_code == 127 or (failed and "command not found" in output):
        return "command not found"
    if exit_code == 126:
        return "not executable"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    parts = [part for part in (_test_counts(lines), _error_line(lines) if failed else None) if part]
    if not parts and failed and lines:
        parts = [lines[-1]]
    return _truncate(" · ".join(parts)) if parts else None


def read_only_label(payload: dict[str, Any]) -> str | None:
    """`cat a.py && ls` -> 'a.py, list files' when every action only reads; otherwise None."""
    actions = payload.get("actions") or []
    if not actions or any(action.get("type") not in READ_ONLY_ACTIONS for action in actions):
        return None
    labels: list[str] = []
    for action in actions:
        if action["type"] == "read":
            label = os.path.basename(action.get("path") or "") or action.get("command") or "file"
        elif action["type"] == "listFiles":
            label = "list files"
        else:
            label = f"search: {action.get('command')}"
        if label not in labels:
            labels.append(label)
    return _truncate(", ".join(labels))


def _test_counts(lines: list[str]) -> str | None:
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        ran = UNITTEST_RAN.match(line)
        if ran:
            total = int(ran[1])
            noun = "test" if total == 1 else "tests"
            verdict = lines[index + 1] if index + 1 < len(lines) else ""
            if verdict.startswith("OK"):
                return f"{total} {noun} passed"
            failed = UNITTEST_FAILED.match(verdict)
            if failed:
                counts = dict(part.split("=") for part in failed[1].split(", ") if "=" in part)
                problems = [f"{counts[k]} {label}" for k, label in
                            (("failures", "failed"), ("errors", "errored")) if k in counts]
                return f"{total} {noun}, " + ", ".join(problems)
            return f"{total} {noun}"
        for pattern in (PYTEST_SUMMARY, JEST_SUMMARY):
            match = pattern.match(line)
            if match:
                return match[1]
    return None


def _error_line(lines: list[str]) -> str | None:
    for line in reversed(lines):
        for pattern in ERROR_LINES:
            match = pattern.match(line)
            if match:
                return match[1]
    return None


def _truncate(text: str) -> str:
    return text if len(text) <= LIMIT else text[: LIMIT - 1] + "…"
