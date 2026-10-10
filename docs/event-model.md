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

Commands carry `payload.actions`, each typed `read`, `list`, `search` or `other`. Adapters map their agent's own names onto these.

## Sources

`agent_history/sources.py` registers every source of history, and is the only place adapters are named. A `Source` has a name, a priority, a way to discover its records on this machine, a test for whether a path is one of its records, and the adapter that normalizes it. Supporting a new agent means writing its adapter and adding one `Source`.

| source | priority | discovered automatically | adapter |
|---|---|---|---|
| `codex-capture` | 2 | no (`ah ingest <capture dir>`) | `adapters/codex.py` |
| `codex-log` | 1 | `$CODEX_HOME/sessions/**/rollout-*.jsonl` (default `~/.codex`) | `adapters/codex_rollout.py` |
| `claude-code-log` | 1 | `$CLAUDE_CONFIG_DIR/projects/*/*.jsonl` (default `~/.claude`) | `adapters/claude_code.py` |
| `opencode-db` | 1 | every top-level session in `$XDG_DATA_HOME/opencode/opencode.db` (default `~/.local/share`), as `<database>/<session id>` | `adapters/opencode.py` |

`ah log` and `ah blame` sync every source first, re-reading only files whose size or modification time changed. When adapters change what they produce, `model.VERSION` is bumped. The next sync then rebuilds every stored session from the source path it remembers, so old sessions never keep stale normalized data. When two sources hold the same agent run (they share thread ids), the higher priority wins: a lower-priority record is skipped, and a higher-priority one replaces what's stored. At equal priority, the record with the most recent activity wins (a resumed Claude Code conversation copies its history into a new transcript; the newer, longer copy is kept). A session is never stored twice.

## Codex App Server mapping

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

## Codex session log mapping

Source: the log Codex writes for every session, however it was started, at `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Adapter: `agent_history/adapters/codex_rollout.py`. Log items mirror App Server items, so the adapter converts each one to the App Server shape and reuses that mapping.

| Codex log record | canonical |
|---|---|
| `session_meta` | `Session` (`id` = Codex thread id, `cli_version`, `cwd`) |
| `event_msg` `task_started` / `task_complete` / `turn_aborted` | `Turn` opened / `completed` / its `reason` (e.g. `interrupted`); never closed → `incomplete` |
| `event_msg` `item_completed`: `UserMessage`, `AgentMessage`, `Reasoning`, `CommandExecution`, `FileChange`, `Extension` (`web.search`) | as their App Server equivalents. Commands: argv joined and unwrapped, `file://` stripped from `cwd`, `duration` → `duration_ms`, `parsed_cmd` → `actions` |
| a command completed after its turn was aborted (logged as failed, exit code -1) | `command` with status `interrupted` |
| `event_msg` `token_count` | `token_usage` |

**Ignored:** `response_item` (raw model input and output, which duplicates the items and includes encrypted reasoning), `world_state`, `turn_context`, `token_usage_record` and `thread_settings_applied`.

Compared with a capture, a log has no approvals and no streaming. That's why captures have the higher priority.

## Claude Code transcript mapping

Source: the transcript Claude Code writes while it works, `~/.claude/projects/<cwd with / → ->/<session-id>.jsonl`. Adapter: `agent_history/adapters/claude_code.py`.

| Claude Code record | canonical |
|---|---|
| `sessionId`, `version`, `cwd` (from the first records that carry them) | `Session` |
| `uuid` of the conversation's first user/assistant record | `Thread`. Shared by every copy of a resumed conversation, so copies dedupe |
| `type: user` with the person's text | `Turn` (keyed by `promptId`) + `user_message` |
| `[Request interrupted by user…]` | the turn (by `promptId`) becomes `interrupted` |
| a turn whose last reply ended with `stop_reason: end_turn` | `completed`; otherwise `incomplete` |
| assistant `text` block | `agent_message`; the turn's last one gets `phase: final` (Claude Code doesn't label final answers; documented heuristic) |
| assistant `thinking` block | `reasoning` (not shown in stories) |
| `tool_use` + its `tool_result` (paired by id) | one event; status from the result: `completed`, `failed` (`is_error`), `declined` (rejected by the user), `interrupted` |
| `Bash` | `command`. No exit code is recorded: 0 on success, parsed from `"Exit code N"` on failure. Output = stdout + stderr |
| `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | `file_change`. Diff built from `toolUseResult.structuredPatch` (jsdiff hunks; empty ranges renumbered to unified-diff convention); `Write` with `type: create` → `add` with full content |
| `Read`, `NotebookRead`, `Grep`, `Glob`, `LS` | `tool_call` with `actions` (`read` / `search` / `list`), shown as `read …`. File contents are not copied into the store |
| `Task`, `Agent` | `subagent_call` (prompt, description, agent type) |
| any other tool (MCP, `WebFetch`, `TodoWrite`, …) | `tool_call` with arguments and raw result |
| `message.usage` | `token_usage`, **once per API reply**: a reply is split across several records that repeat the same usage |
| `isSidechain: true` records | events in a child thread |

**Ignored:** `isMeta` records (slash-command output), compaction summaries, slash commands themselves (`<command-name>…`), and bookkeeping: `attachment`, `mode`, `permission-mode`, `ai-title`, `last-prompt`, `agent-name`, `atis-latch`, `cost-state`, `queue-operation`, `system`, `file-history-*`.

Claude Code deletes transcripts after about 30 days by default. Sessions already synced stay in `ah`'s database.

Not yet read: sub-agent transcripts under `<session-id>/subagents/`.

## opencode mapping

opencode keeps every session in one SQLite database, opened read-only. A source normally maps one file to one session; here each session gets the virtual path `<database>/<session id>`, and the session's `time_updated` replaces the file's mtime and size to tell whether it changed since the last sync (`Source.stamp`). Records have no line numbers, so each event keeps the opencode part or message id in `payload.record_id` instead of `source_lines`.

| opencode record | canonical |
|---|---|
| top-level `session` row (`id`, `version`, `directory`, times) | `Session` and its main `Thread` (same id) |
| `session` row with `parent_id` (a sub-agent run by `task`) | a child `Thread` (its own id, `parent_thread_id` = `parent_id`) of the top-level session, at any depth. Its turns and events belong to that thread; the session's `ended_at` and sync stamp are the latest over the whole tree, so a sub-agent's activity re-syncs its parent |
| `message` with `role: user` | `Turn` + `user_message`; text from its `text` parts, minus `synthetic` ones (file contents opencode attaches) |
| `message` with `role: assistant` | belongs to the turn named by `parentID`. Turn status from the last reply: `error.name: MessageAbortedError` → `interrupted`, another `error` → `failed` (plus an `error` event), `finish` other than `tool-calls` → `completed`, else `incomplete` |
| `text` part | `agent_message`; the turn's last one gets `phase: final` (opencode doesn't label final answers) |
| `reasoning` part | `reasoning` |
| `tool` part (`callID` → `source_id`) | one event; status from `state.status`: `completed`, `error` → `failed`, `declined` (permission rejected or denied by a rule) or `interrupted` (aborted); `pending`/`running` → `interrupted` in an interrupted turn, else `incomplete` |
| `bash` | `command`; `metadata.exit` → `exit_code` (a non-zero exit makes it `failed`), `metadata.output` → `output` |
| `edit`, `write`, `multiedit`, `patch`, `apply_patch` | `file_change`. Diff built from `metadata.filediff.before`/`after` (full contents), or `metadata.filediff.patch` where newer versions keep only that. `metadata.diff` is only a fallback: opencode strips common indentation from it for display, so its lines aren't the file's. `write` with `metadata.exists: false` → `add` with full content |
| `read`, `grep`, `glob`, `list` | `tool_call` with `actions` (`read` / `search` / `list`). File contents are not copied into the store |
| `task` | `subagent_call` (the child session id from `metadata.sessionId` in `receiver_thread_ids`) |
| any other tool | `tool_call` with arguments and output |
| `step-finish` part `tokens` | `token_usage`, once per model call; `input` includes `cache.read` and `cache.write` |

**Ignored:** `step-start`, `patch` (snapshot hashes), `snapshot`, `file`, `agent`, `compaction` parts.

Normalizing any session of a tree (`ah ingest <db>/<child id>`) normalizes the whole tree from its top-level session.

## Observed changes

Agents only report changes they make with their edit tools, so a shell command's side effects (`printf >> f`, `sed -i`, a script that writes files) are invisible in their logs. `ah setup` therefore installs hooks (`agent_history/setup.py`) that run `ah hook <agent> pre|post|stop` (`agent_history/hooks.py`) around each tool call that can change files. Each run snapshots the workspace (`agent_history/workspace.py`) and records the difference as an **observation**:

- **Snapshots work like git's index.** The workspace is the git top level of the hook's `cwd` (or the directory itself); its files are git's view (tracked and untracked, minus ignored). Every file is `stat`ed, and only files whose size or modification time changed (or that changed within 2 s of the last snapshot) are read. Contents go into a content-addressed store, `objects/` next to the database: text files up to 1 MB. Binary and larger files are tracked by hash only.
- **The first snapshot of a workspace is a `baseline`:** what it held before anything was observed.
- **`pre` records changes since the last snapshot as `outside`:** made by a person or another program, not an agent. Then an `agent` observation opens for the call. If another call is still running, those changes go to it instead.
- **`post` records changes since `pre` as that call's,** and closes it. A `post` without a `pre` still records.
- **`stop`** (agent turn ended, or a new prompt) closes calls that never got their `post`, such as interrupted ones.
- **Overlapping observations in one workspace** (parallel tool calls, two agents at once) are marked `concurrent`.
- **Hooks never disturb the agent.** They're serialized with a file lock, always exit 0, never write to stdout, and log failures to `hooks.log` next to the database.

Hook input is the same for every agent (`session_id`, `tool_use_id`, `tool_name`, `tool_input`, `cwd`, …; Codex adds `turn_id`); parsers live in `hooks.PARSERS`. opencode has no hook settings: its hooks are JS plugins, so `ah setup` writes one, `$XDG_CONFIG_HOME/opencode/plugin/agent-history.js` (default `~/.config`), tagged on its first line. It runs `ah hook opencode pre|post` from `tool.execute.before`/`after` (every tool) and `stop` on `session.idle` and `chat.message`, sending the same fields: `session_id` = opencode's `sessionID`, `tool_use_id` = its `callID`, `cwd` = the plugin's `directory`. Verified with real hook calls:
- **Claude Code:** `tool_use_id` is the transcript's `tool_use.id` (`toolu_…`), and `session_id` is its `sessionId`.
- **Codex:** `tool_use_id` is the item id in its session log (`exec-…`), and `session_id` is the thread id.
- **Shell tool name:** both agents call it `Bash`.

An `agent` observation links to the event whose `payload.source_id` equals the hook's `tool_use_id`, in the same session or thread.

## Blame

`ah blame` replays every non-declined `FileChange` in time order into a per-file line-origin map. `add` attributes every line. `update` hunks are applied by their line numbers, so the file's original content is not needed, and lines no recorded change touched stay unattributed. A replaced line keeps the history of the line it replaced, so `ah blame file:N` can show a line being written, broken and fixed.

Each line also remembers its line number right after the edit that wrote it, so a story marks exactly that line in the edit's diff (not just the first line with the same text). For a large edit, such as creating a file, the diff is shown as a window around the line.

Observed changes join the same timeline:

- **Aligning instead of patching.** For an observed change, the replay is aligned (`difflib`) to the file's actual content after the change. Lines that stayed keep their author; new or changed lines belong to that observation, and carry the history of the lines they replaced. Because the content is ground truth, the replay corrects itself.
- **No double counting.** An observed tool call replaces the same call's recorded changes for that file.
- **Sub-agents.** A line written in a child thread is credited to its session, with the turn the person asked in (found through the `subagent_call` whose `receiver_thread_ids` names the thread), and the story shows each hand-off: `turn 1 › sub-agent 22222222`, the person's prompt, then the prompt each sub-agent was given. `ah log` labels a sub-agent's turns `sub-agent <id>, from turn N`.
- **Pre-observation history survives.** At the baseline, the replay is aligned to the baseline content: lines agents wrote before observation began keep their author, and the rest become known pre-existing lines.
- **Every line gets an author:**
  - the agent, linked to its event when its transcript is synced
  - outside any agent (`you`)
  - pre-existing (`·`)
  - untracked, when nothing recorded or observed it
- **Several agents in one file.** Line history entries name their agent, so a file several agents worked on reads at a glance.

When the file exists on disk, the result is aligned with its current content. A line changed after the last recorded or observed change is reported as untracked, never blamed on whatever change last touched that position.

### The story behind a line

`ah blame file:N` prints the turn that wrote the line as an ordered list of steps: the prompt, the agent's messages, files it read, edits, and commands. Up to 12 steps before the change are shown, then the change itself (marked `▶`), then the steps after it up to the next test run, then the turn's final answer. Order is not proof of cause, and the output says so.

Agent messages are shown in full. Every edit shows its diff: the edit that wrote the line shows its full diff for that file, with the line marked `← this line`, and other edits show their first 12 diff lines. So a change to a test just before the fix is visible, not just named. Diffs are coloured when writing to a terminal (`NO_COLOR` turns this off).

### Evidence from commands

`agent_history/commands.py` reads the canonical command payload, so it works for any agent:

- **Test runners** are recognised at the start of a command segment (`pytest`, `python -m unittest`, `npm test`, `go test`, `cargo test`, …, also behind `cd x &&`, `uv run`, `npx`). `rg pytest` is not a test run.
- **Summaries** come from the end of the output, because agents may truncate the start: unittest/pytest/jest counts, the last assertion or exception line, `command not found` for exit 127, or the last output line as a fallback.
- **Read-only commands** (every action is a read, list or search) are shown as `read shop.py, list files`.

**Known gap:** edits made through shell commands (`sed -i`, `echo >> file`) aren't `fileChange` items, so blame can't see them yet. `turn/diff/updated` is the planned source for recovering them.

