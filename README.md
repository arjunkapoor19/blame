# agent-history

An observability and history layer for AI coding agents. Git tells you what changed; Agent History tells you what the agent did that caused those changes.

It reads the session history **Codex**, **Claude Code** and **opencode** already write, normalizes them into one agent-agnostic event model stored in SQLite, and answers "why does this line exist?" with `ah blame`, whichever agent wrote the line. Use your agents exactly as you normally do; there is nothing to set up. An optional recorder captures richer detail through the Codex App Server protocol.

```text
$ ah blame shop.py:6
shop.py:6
        return price * (1 - percent / 100)

written by codex · session 20261004T132159Z-39de3d · turn 1 · 2026-10-04 18:52:33 IST

You asked: "apply_discount in shop.py is buggy. Write unittest tests in test_shop.py that expose it, run them and watch them fail, then fix the bug and rerun until they pass."

  18:52:20  edit     +test_shop.py
                     │ +import unittest
                     │ …
  18:52:20  test     ✗ python -m unittest -v  → command not found
  18:52:23  test     ✗ python3 -m unittest -v  → 3 tests, 1 failed · AssertionError: -25 != 0
  18:52:26  agent    The first run exposed one bug, but the 20% test used a $100 price, which
                     accidentally passed the current subtraction implementation. I'm strengthening
                     that case with a different price so it checks percentage math directly, then
                     I'll correct `apply_discount`.
  18:52:29  edit     ~test_shop.py
                     │ @@ -7,3 +7,3 @@
                     │      def test_applies_percentage_to_price(self):
                     │ -        self.assertEqual(apply_discount(100, 20), 80)
                     │ +        self.assertEqual(apply_discount(200, 20), 160)
  18:52:30  test     ✗ python3 -m unittest -v  → 3 tests, 2 failed · AssertionError: -25 != 0
▶ 18:52:33  edit     ~shop.py   ← wrote this line
                     │ @@ -5,3 +5,3 @@
                     │  def apply_discount(price, percent):
                     │ -    return price - percent
                     │ +    return price * (1 - percent / 100)   ← this line
  18:52:34  test     ✓ python3 -m unittest -v  → 3 tests passed
```

## Layout

```
recorder/codex_recorder.py   records one Codex session (stdlib-only Python 3.9+)
agent_history/               canonical model, SQLite store, blame, `ah` CLI
  sources.py                 every source of agent history, and syncing them
  adapters/codex.py          Codex recorder capture -> canonical events
  adapters/codex_rollout.py  Codex's own session logs -> canonical events
  adapters/claude_code.py    Claude Code session transcripts -> canonical events
  adapters/opencode.py       opencode's session database -> canonical events
tests/                       pytest suite; fixtures/ holds sanitized real captures
codex-schema/                App Server schemas generated from the installed Codex (currently 0.155.1)
docs/event-model.md          the canonical model, each agent's mapping, and how blame works
docs/protocol-notes.md       what we've learned about the protocol from real captures
captures/                    one directory per recorded session (gitignored; may contain code and prompts)
```

## Using the history

Requires [uv](https://docs.astral.sh/uv/). There are no runtime dependencies beyond the Python standard library.

```sh
uv tool install --editable .                  # puts `ah` on your PATH
ah setup                                      # once: let ah observe Claude Code, Codex and opencode tool calls
codex                                         # or `claude` or `opencode`: use your agent as usual, then:
ah blame path/to/file.py:42                   # why does line 42 exist? (picks up new sessions automatically)
```

`ah setup` adds small hooks to your user-level Claude Code and Codex settings (`~/.claude/settings.json`, `~/.codex/hooks.json`) and a small opencode plugin (`~/.config/opencode/plugin/agent-history.js`). Before and after every tool call that can change files, they snapshot the project, so `ah` sees exactly what each call changed, including edits made through the shell (`printf >> f`, `sed -i`, scripts), which agents don't report. Edits made between tool calls are labeled as made outside any agent (`you`). Each hook takes about 0.1 s. Your settings are backed up as `*.agent-history.bak`, and `ah setup --remove` takes the hooks out again. Codex asks you to trust new hooks once; restart opencode to load the plugin. Without `ah setup`, `ah` still works from agents' logs, but can't see shell edits.

Snapshots keep copies of text files up to 1 MB under `~/.agent-history/objects`, next to the database. They never leave your machine.

```sh
uv run ah log                                 # list sessions
uv run ah log <session>                       # one session's timeline (any unique part of the id)
uv run ah blame path/to/file.py               # who wrote each line: which agent (or you), session, turn, when
uv run ah blame path/to/file.py:42            # one line: the story of the turn that wrote it
uv run ah ingest                              # sync agent logs explicitly and list what was loaded
uv run ah ingest captures/<session-dir>       # load a recorder capture (wins over the log of the same session)
uv run pytest                                 # tests
```

`ah log` and `ah blame` first sync your agents' session logs (Codex: `$CODEX_HOME/sessions`, default `~/.codex/sessions`; Claude Code: `$CLAUDE_CONFIG_DIR/projects`, default `~/.claude/projects`; opencode: `~/.local/share/opencode/opencode.db`, read-only), reading only new or changed sessions. History survives your agent's own cleanup: Claude Code, for example, deletes transcripts after about 30 days, but synced sessions stay in `ah`. The database defaults to `~/.agent-history/history.db`. Override it with `--db` or `$AGENT_HISTORY_DB`. Each agent run is stored once, even if it was both logged and captured.

Blame aligns everything with the file on disk, so a line nothing recorded or observed is reported as untracked, never misattributed. See `docs/event-model.md` for how it all fits together.

## Recording a session (optional)

Codex's own logs are enough for `ah blame`. The recorder adds approvals, commands still running when a turn was interrupted, and the full streaming protocol.

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
