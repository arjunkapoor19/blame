import pytest

from agent_history.commands import is_test, read_only_label, summarize, verdict

UNITTEST_FAIL = """\
---------------------------------------------------------
Traceback (most recent call last):
  File "/workspace/test_shop.py", line 14, in test_full_discount_reduces_price_to_zero
    self.assertEqual(apply_discount(75, 100), 0)
AssertionError: -25 != 0

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=2)
"""
UNITTEST_ERROR = """\
    return sum(prices) / len(prices)
ZeroDivisionError: division by zero

----------------------------------------------------------------------
Ran 4 tests in 0.001s

FAILED (errors=1)
"""
UNITTEST_OK = "test_a (t.T) ... ok\n\n------\nRan 1 test in 0.000s\n\nOK\n"
PYTEST_FAIL = """\
E       assert 1 == 2
FAILED tests/test_x.py::test_y - assert 1 == 2
========================= 1 failed, 4 passed in 0.12s =========================
"""
JEST_FAIL = "  ● sum › adds\n\nTests:       1 failed, 4 passed, 5 total\nTime:        0.5 s\n"


def command(output, exit_code, cmd="python3 -m unittest -v"):
    return {"command": cmd, "exit_code": exit_code, "output": output}


@pytest.mark.parametrize("payload, status, expected", [
    (command(UNITTEST_FAIL, 1), "failed", "3 tests, 2 failed · AssertionError: -25 != 0"),
    (command(UNITTEST_ERROR, 1), "failed", "4 tests, 1 errored · ZeroDivisionError: division by zero"),
    (command(UNITTEST_OK, 0), "completed", "1 test passed"),
    (command(PYTEST_FAIL, 1, "pytest"), "failed", "1 failed, 4 passed · tests/test_x.py::test_y - assert 1 == 2"),
    (command(JEST_FAIL, 1, "npx jest"), "failed", "1 failed, 4 passed, 5 total"),
    (command("zsh:1: command not found: python\n", 127), "failed", "command not found"),
    (command("", 126), "failed", "not executable"),
    (command(None, None), "declined", "declined"),
    (command(None, None), "interrupted", "interrupted"),
    (command("building...\nerror: linker failed\n", 1, "make"), "failed", "error: linker failed"),
    (command("some\nlast line\n", 2, "./run.sh"), "failed", "last line"),
    (command("hello\n", 0, "echo hello"), "completed", None),
])
def test_summarize(payload, status, expected):
    assert summarize(payload, status) == expected


def test_summarize_survives_head_truncated_output():
    # Agents may cut the start of long output; the summary comes from the end.
    assert summarize(command(UNITTEST_FAIL[200:], 1), "failed") == "3 tests, 2 failed · AssertionError: -25 != 0"


def test_summary_is_truncated():
    assert len(summarize(command("x" * 500, 1, "./run.sh"), "failed")) == 100


@pytest.mark.parametrize("cmd", [
    "pytest", "pytest -q tests/", "uv run pytest", "python3 -m unittest -v", "python3.11 -m pytest",
    "cd app && npm test", "pnpm run test", "npx vitest run", "go test ./...", "cargo test",
    "./gradlew clean test", "make test", "tox -e py39", "CI=1 npx jest", ".venv/bin/pytest -x",
    "ls; python3 -m unittest", "(cd web && pnpm test)",
])
def test_is_test(cmd):
    assert is_test(cmd)


@pytest.mark.parametrize("cmd", [
    "cat test_shop.py", "ls tests", "python3 test_shop.py", "rg pytest", "grep -r \"npm test\" .", "echo testing",
    "git log --grep=jest", "", None,
])
def test_is_not_test(cmd):
    assert not is_test(cmd)


def test_verdict():
    assert verdict(command("", 0), "completed") == "passed"
    assert verdict(command("", 1), "failed") == "failed"
    assert verdict(command("", 127), "failed") is None
    assert verdict(command(None, None), "declined") is None
    assert verdict(command("", 0, "ls"), "completed") is None


def test_read_only_label():
    actions = [{"type": "read", "path": "/w/shop.py"}, {"type": "list", "command": "ls"},
               {"type": "read", "path": "/w/shop.py"}]
    assert read_only_label({"actions": actions}) == "shop.py, list files"
    assert read_only_label({"actions": [{"type": "search", "command": "rg add"}]}) == "search: rg add"
    assert read_only_label({"actions": actions + [{"type": "other"}]}) is None
    assert read_only_label({"actions": []}) is None
