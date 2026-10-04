# agent-history

An observability and history layer for AI coding agents. The current prototype records **Codex** sessions through the Codex App Server's JSON-RPC protocol (stdio transport). It doesn't scrape terminal output or modify Codex.

Current stage: **raw capture**, i.e. protocol archaeology. There is no storage layer, UI, or analysis yet.

## Layout

```
recorder/codex_recorder.py   records one Codex session (stdlib-only Python 3.9+)
codex-schema/                App Server schemas generated from the installed Codex (currently 0.155.1)
docs/protocol-notes.md       what we've learned about the protocol from real captures
captures/                    one directory per recorded session (gitignored; may contain code and prompts)
```

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
