# Canonical event model

Adapters translate an agent's native records into one agent-agnostic model (`agent_history/model.py`). Everything above the adapters (store, blame, and later diff and replay) sees only this model. If a field only makes sense for one agent, it goes in an event's `payload`, never in the model.

The raw capture stays the source of truth. Every event keeps `source_lines`, the 1-based lines in the raw source it came from, so any canonical event can be traced back to the exact bytes the agent emitted.

## Entities

```text
Session            one recorded run of an agent (agent, version, cwd, outcome, source)
 └─ Thread         one agent conversation; sub-agents are child threads (parent_thread_id)
     └─ Turn       one user request and everything done to answer it (input_text, status)
         └─ Event  one thing that happened (kind, status, timestamps, payload, parent_id)
             └─ FileChange   path, kind (add | update | delete | move), diff
```

Timestamps are integer milliseconds since the Unix epoch (UTC). Event `seq` is the order within a session.

## Event kinds

| kind | meaning | key payload fields |
|---|---|---|
| `user_message` | input from the user | `text` |
| `agent_message` | text from the agent | `text`, `phase` (`commentary`, `final`, `plan`) |
| `reasoning` | model reasoning, when exposed | `summary`, `content` |
| `command` | a shell command | `command`, `cwd`, `exit_code`, `output`, `duration_ms`, `actions` |
| `file_change` | edits to files (rows in `file_changes`) | `paths` |
| `tool_call` | any other tool (MCP, web search, ...) and any unrecognised record | `tool`, `server`, `arguments`, `result` / `raw` |
| `approval` | a permission request and its answer; `parent_id` is the gated event | `decision`, `subject`, `command` |
| `subagent_call` | spawning or messaging another agent | `action`, `prompt`, `receiver_thread_ids` |
| `token_usage` | cumulative and per-step token counts | `total`, `last`, `context_window` |
| `error` | an agent or API error | `message`, `will_retry` |

Statuses: `in_progress`, `completed`, `failed`, `declined`, `interrupted` (the turn was interrupted mid-item), `incomplete` (the item never finished and the turn didn't explain why). Approvals use `approved` / `declined`.

Rule: an adapter never drops an agent record it doesn't understand. It becomes a `tool_call` with the native record in `payload.raw`.

## Codex mapping

Source: Codex App Server JSON-RPC (see `protocol-notes.md`), adapter `agent_history/adapters/codex.py`.

| Codex | canonical |
|---|---|
| `thread/start` response, `thread/started` | `Thread` (`parentThreadId` → `parent_thread_id`) |
| `turn/started`, `turn/completed` | `Turn` (input text from the turn's first `userMessage`) |
| `item/started` + `item/completed` (paired by `item.id`) | one `Event` with both raw lines in `source_lines` |
| `userMessage` | `user_message` |
| `agentMessage` (`commentary` / `final_answer`), `plan` | `agent_message` (`commentary` / `final` / `plan`) |
| `reasoning` | `reasoning` |
| `commandExecution` | `command`; `/bin/zsh -lc '…'` unwrapped, original kept in `raw_command` |
| `fileChange` | `file_change` + one `FileChange` per entry; `update` with `move_path` → `move` |
| `mcpToolCall`, `dynamicToolCall`, `functionCallOutput`, `webSearch` | `tool_call` |
| `collabAgentToolCall`, `subAgentActivity` | `subagent_call`; receivers become child threads |
| any other item type | `tool_call` with `payload.raw` |
| `item/*/requestApproval`, `execCommandApproval`, `applyPatchApproval` | `approval`, decision recovered from `wire.jsonl` |
| `thread/tokenUsage/updated` | `token_usage` |
| `error` | `error` |

**Dropped**, but still available in the raw capture:

- `item/*/delta` streaming chunks. These are usually most of a capture, and the full text repeats on `item/completed`.
- `turn/diff/updated`. This is the turn's cumulative git diff, kept for later recovery of edits made through shell commands.
- `thread/status/changed`, `serverRequest/resolved`, `account/rateLimits/updated`, `mcpServer/startupStatus/updated`, `remoteControl/status/changed`, and JSON-RPC responses. These are plumbing.

## Blame

`ah blame` replays every non-declined `FileChange` in time order into a per-file line-origin map. `add` attributes every line. `update` hunks are applied by their line numbers, so the file's original content is not needed, and lines no recorded change touched stay unattributed. A replaced line keeps the history of the line it replaced, so `ah blame file:N` can show a line being written, broken and fixed.

When the file exists on disk, the replayed lines are aligned with its current content (`difflib`). Lines a human added or edited after the agent finished are reported as not written by a recorded agent, instead of being blamed on whatever agent change last touched that position.

### The story behind a line

`ah blame file:N` prints the turn that wrote the line as an ordered list of steps: the prompt, the agent's messages, files it read, edits, and commands. Up to 12 steps before the change are shown, then the change itself (marked `▶`), then the steps after it up to the next test run, then the turn's final answer. Order is not proof of cause, and the output says so.

### Evidence from commands

`agent_history/commands.py` reads the canonical command payload, so it works for any agent:

- **Test runners** are recognised at the start of a command segment (`pytest`, `python -m unittest`, `npm test`, `go test`, `cargo test`, …, also behind `cd x &&`, `uv run`, `npx`). `rg pytest` is not a test run.
- **Summaries** come from the end of the output, because agents may truncate the start: unittest/pytest/jest counts, the last assertion or exception line, `command not found` for exit 127, or the last output line as a fallback.
- **Read-only commands** (every action is a read, list or search) are shown as `read shop.py, list files`.

**Known gap:** edits made through shell commands (`sed -i`, `echo >> file`) aren't `fileChange` items, so blame can't see them yet. `turn/diff/updated` is the planned source for recovering them.
