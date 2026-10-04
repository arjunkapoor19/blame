# agent-history

An observability and history layer for AI coding agents. Git tells you what changed; Agent History tells you what the agent did that caused those changes.

The current prototype records **Codex** sessions through the Codex App Server's JSON-RPC protocol (stdio transport), normalizes them into an agent-agnostic event model stored in SQLite, and answers "why does this line exist?" with `ah blame`. It doesn't scrape terminal output or modify Codex.

```text
$ ah blame shop.py:6
shop.py:6
        return price * (1 - percent / 100)

✓ tests passed after this change  (python3 -m unittest -v → 4 tests passed)
written by codex · session 20261004T132159Z-39de3d · turn 1 · 2026-10-04 13:22:33 UTC

You asked: "apply_discount in shop.py is buggy. Write unittest tests in test_shop.py that expose it, run them and watch them fail, then fix the bug and rerun until they pass."

  13:22:12  read     shop.py, list files
  13:22:20  edit     +test_shop.py
  13:22:20  test     ✗ python -m unittest -v  → command not found
  13:22:23  test     ✗ python3 -m unittest -v  → 3 tests, 1 failed · AssertionError: -25 != 0
  13:22:26  agent    The first run exposed one bug, but the 20% test used a $100 price, which accidentally passed the cu…
  13:22:29  edit     ~test_shop.py
  13:22:30  test     ✗ python3 -m unittest -v  → 3 tests, 2 failed · AssertionError: -25 != 0
▶ 13:22:33  edit     ~shop.py   ← wrote this line
  13:22:34  test     ✓ python3 -m unittest -v  → 3 tests passed
```

`ah blame shop.py` marks every agent-written line ✓ (tests passed after it), ✗ (tests failing after it), ○ (never tested) or ⚠ (rewritten 2+ times), so you can see which lines the agent wrote but never checked.

## Layout

```
recorder/codex_recorder.py   records one Codex session (stdlib-only Python 3.9+)
agent_history/               canonical model, adapters, SQLite store, blame, `ah` CLI
  adapters/codex.py          Codex capture -> canonical events
tests/                       pytest suite; fixtures/ holds sanitized real captures
codex-schema/                App Server schemas generated from the installed Codex (currently 0.155.1)
docs/event-model.md          the canonical model, the Codex mapping, and how blame works
docs/protocol-notes.md       what we've learned about the protocol from real captures
captures/                    one directory per recorded session (gitignored; may contain code and prompts)
```

## Using the history

Requires [uv](https://docs.astral.sh/uv/). There are no runtime dependencies beyond the Python standard library.

```sh
uv sync
uv run ah ingest captures/<session-dir>...    # normalize captures into the database
uv run ah log                                 # list sessions
uv run ah log <session>                       # one session's timeline (any unique part of the id)
uv run ah blame path/to/file.py               # which agent event wrote each line, and was it tested
uv run ah blame path/to/file.py:42            # one line: the story of the turn that wrote it
uv run pytest                                 # tests
```

The database defaults to `~/.agent-history/history.db`. Override it with `--db` or `$AGENT_HISTORY_DB`. Re-ingesting a capture replaces it.

Blame aligns recorded history with the file on disk, so lines edited by hand afterwards aren't blamed on the agent. It can't yet see edits the agent made through shell commands (`sed -i`, `echo >>`); see `docs/event-model.md`.

## Recording a session

```sh
python3 recorder/codex_recorder.py --cwd /path/to/project 'first prompt' 'follow-up prompt'
python3 recorder/codex_recorder.py --cwd /path/to/project -i            # type prompts turn by turn
```

Each prompt runs as a separate turn on the same thread. Codex edits files in `--cwd` (default sandbox: `workspace-write`).

| Option | Default | Purpose |
|---|---|---|
| `--cwd` | current dir | workspace Codex operates in |
| `--model` | Codex config | model requested in `thread/start` |
| `--sandbox` | `workspace-write` | `read-only`, `workspace-write`, `danger-full-access` |
| `--approval-policy` | `never` | `untrusted`, `on-request`, `never` |
| `--approvals` | `decline` | how to answer approval requests: `decline`, `accept`, `ask` (prompt in the terminal) |
| `--turn-timeout` | 1800 | total seconds per turn |
| `--rpc-timeout` | 120 | seconds to wait for each request's response |
| `--captures` | `$AGENT_HISTORY_CAPTURES` or `./captures` | where session directories go |
| `--codex` | `$CODEX_BIN` or `codex` | Codex executable |

Environment variables are optional; see `.env.example`. The recorder doesn't load `.env` itself, so use `set -a; source .env; set +a` if you want it.

**Ctrl-C** during a turn sends `turn/interrupt`, and the turn ends with `status=interrupted`. A second Ctrl-C aborts immediately.

## Capture format

`captures/<UTC time>-<id>/`:

- **`events.jsonl`**: every line Codex wrote to stdout, byte-for-byte. This is the source of truth.
- **`wire.jsonl`**: the ordered transcript of both directions plus Codex stderr: `{"seq", "ts", "dir": "send"|"recv"|"stderr", "raw"}`. `ts` is when the recorder read or wrote the line. Order between stdout and stderr is approximate because they're separate pipes.
- **`session.json`**: Codex version, settings, `thread_id`, per-turn `{prompt, turn_id, status, started_at, ended_at}`, `outcome` (`completed` / `interrupted` / `aborted` / `codex_exited` / `timeout` / `error`), Codex exit code and last lines of stderr. It's rewritten after every state change, so it's still useful if the recorder crashes.

Recorder progress goes to stderr. It never mixes with the protocol stream.

## Updating the schema

After upgrading Codex:

```sh
codex app-server generate-json-schema --experimental --out ./codex-schema
```

Then check the diff and add anything relevant to `docs/protocol-notes.md`.
