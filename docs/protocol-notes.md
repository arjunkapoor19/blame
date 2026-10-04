# Codex App Server protocol notes

These notes come from `codex-schema/` and from real captures, and cover what the recorder and the Codex adapter rely on. How these messages map to canonical events is in [`event-model.md`](event-model.md). Verified against **codex-cli 0.155.1**.

## Framing

- JSON-RPC over stdio, one JSON object per line. The server **omits** the `"jsonrpc": "2.0"` member (see `JSONRPCMessage.json`), and the recorder does the same.
- Three message shapes arrive on stdout: responses (`id` + `result`/`error`), notifications (`method`, no `id`), and **server requests** (`method` + `id`) that the client must answer.
- Server request IDs have their own numbering, which **starts at 0**. Test whether the `id` key exists, not whether the value is truthy.
- Every notification carries a server timestamp, `emittedAtMs`. Responses and server requests don't.

## Handshake and session flow

1. `initialize`: `clientInfo` is required. `capabilities.experimentalApi: true` opts into experimental methods and fields.
2. `initialized`: a client **notification** (`ClientNotification.json`) sent after the initialize response.
3. `thread/start` with `cwd`, `approvalPolicy`, `sandbox` and optionally `model`. The response includes `thread.id`.
4. `turn/start` with `threadId` and `input: [{type: "text", text}]`. The response includes `turn.id`.
5. Read notifications until `turn/completed` for that turn ID. `turn.status` is one of `completed | interrupted | failed | inProgress`.
6. More `turn/start` calls on the same thread give a multi-turn session.
7. `turn/interrupt {threadId, turnId}` ends the turn with `status: interrupted`.
8. Closing stdin makes the server exit cleanly (exit code 0).

## Items

Agent activity arrives as `item/started` / `item/completed` pairs keyed by `item.id`. The types seen so far: `userMessage`, `agentMessage` (`phase`: `commentary` | `final_answer`), `reasoning` (summary/content often empty), `commandExecution`, `fileChange`. The schema also defines `mcpToolCall`, `dynamicToolCall`, `webSearch`, `plan`, `collabAgentToolCall`, `subAgentActivity`, `contextCompaction` and others.

- **Reading files appears as a `commandExecution`.** The command's `commandActions` classify it, e.g. `{type:"read", path}` or `{type:"listFiles"}`. Anything unrecognized is `{type:"unknown"}`, which is how test runs show up.
- `commandExecution` has `command` (wrapped as `/bin/zsh -lc '…'`), `cwd`, `exitCode`, `aggregatedOutput`, `durationMs` and `status` (`completed`, `declined`, …). `durationMs` is often `0` for fast commands.
- `fileChange.changes[]` holds `{path, kind: {type: add|update|…}, diff}`.
- `turn/diff/updated` streams the turn's cumulative diff.
- **An interrupted turn leaves dangling items**: an `item/started` with no `item/completed`. The adapter closes them as `interrupted` when the turn ends.

## Streaming

`item/agentMessage/delta` and `item/commandExecution/outputDelta` stream partial text and are usually the bulk of the events. The full text is repeated on `item/completed`.

## Approvals

With `approvalPolicy: untrusted` or `on-request`, Codex sends server requests such as `item/commandExecution/requestApproval` and `item/fileChange/requestApproval` (reply `{decision: accept|decline|…}`), or the legacy `execCommandApproval` / `applyPatchApproval` (reply `{decision: approved|denied|…}`). After the reply, Codex sends `serverRequest/resolved {requestId}`. A declined command finishes as `commandExecution` with `status: declined` and a null `exitCode`.

## Other notifications seen

`thread/started`, `thread/status/changed`, `turn/started`, `thread/tokenUsage/updated`, `account/rateLimits/updated`, `mcpServer/startupStatus/updated`, `remoteControl/status/changed`, `item/commandExecution/terminalInteraction`.

## Schema drift 0.153.4 → 0.155.1

The changes were additive only. No methods the recorder uses were changed.
- New client requests: `memory/status`, `thread/attachment/{add,list,remove}`, `userVerification/{cancel,delete,enroll,status,verify}`
- New notification: `thread/attachment/updated`
- Server requests: unchanged

## Open questions

- What do `mcpToolCall`, `webSearch`, `plan` and `contextCompaction` items look like in practice?
- How does `turn/steer` (adding input mid-turn) appear in the stream?
- What does a `failed` turn look like (model/API error, `error` notification with `willRetry`)?
- Can `experimentalRawEvents` (raw Responses API items) show more than the item stream does?
