#!/usr/bin/env python3
"""Record a Codex App Server v2 session as raw JSON-RPC.

Each run writes one capture directory:

  events.jsonl  every line Codex wrote to its stdout, byte-for-byte, one message per line
  wire.jsonl    ordered transcript of both directions plus Codex stderr, with timestamps
  session.json  what was run: Codex version, settings, thread/turn ids, prompts, outcome

Protocol flow (see codex-schema/, v2):
  initialize -> initialized -> thread/start -> (turn/start ... turn/completed)+

The App Server's stdout is protocol-only. Everything this program prints goes to
its own stderr, so it can never be confused with recorded protocol traffic.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

RECORDER_VERSION = "0.2.0"
DEFAULT_CAPTURES = Path(__file__).resolve().parent.parent / "captures"

# Server-initiated approval requests and the (approve, deny) decision values
# their response schemas accept. Anything else gets a JSON-RPC error reply.
APPROVALS = {
    "item/commandExecution/requestApproval": ("accept", "decline"),
    "item/fileChange/requestApproval": ("accept", "decline"),
    "execCommandApproval": ("approved", "denied"),
    "applyPatchApproval": ("approved", "denied"),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def note(text: str) -> None:
    print(f"[recorder] {text}", file=sys.stderr, flush=True)


class CodexExited(RuntimeError):
    pass


class Capture:
    """One capture directory. Writes from all threads go through a single lock."""

    def __init__(self, directory: Path, meta: dict[str, Any]) -> None:
        directory.mkdir(parents=True)
        self.dir = directory
        self.meta = meta
        self._events = (directory / "events.jsonl").open("wb")
        self._wire = (directory / "wire.jsonl").open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self.save_meta()

    def record(self, direction: str, raw: bytes) -> None:
        ts = now()
        with self._lock:
            if self._closed:
                return
            if direction == "recv":
                self._events.write(raw if raw.endswith(b"\n") else raw + b"\n")
                self._events.flush()
            self._seq += 1
            entry = {"seq": self._seq, "ts": ts, "dir": direction,
                     "raw": raw.rstrip(b"\n").decode("utf-8", errors="replace")}
            self._wire.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._wire.flush()

    def save_meta(self) -> None:
        tmp = self.dir / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.dir / "session.json")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._events.close()
            self._wire.close()


class AppServer:
    def __init__(self, codex: str, capture: Capture, approvals: str) -> None:
        self.capture = capture
        self.approvals = approvals
        # New session: a terminal Ctrl-C reaches only the recorder, which turns
        # it into a protocol-level turn/interrupt instead of killing Codex.
        self.process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self.suppressed_deltas = 0
        self._next_id = 1
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout
        for raw in self.process.stdout:
            self.capture.record("recv", raw)
            try:
                message = json.loads(raw)
            except ValueError as error:  # includes JSONDecodeError and UnicodeDecodeError
                note(f"non-JSON line from Codex recorded as-is: {error}")
                continue
            if isinstance(message, dict):
                self.inbox.put(message)
            else:
                note("non-object JSON-RPC message recorded as-is")
        self.inbox.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr
        for raw in self.process.stderr:
            self.capture.record("stderr", raw)
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                note(f"codex stderr: {text}")

    def new_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def send(self, message: dict[str, Any]) -> None:
        # The server omits the "jsonrpc" member (see codex-schema/JSONRPC*.json); so do we.
        wire = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        self.capture.record("send", wire)
        assert self.process.stdin
        try:
            self.process.stdin.write(wire)
            self.process.stdin.flush()
        except OSError as error:
            raise CodexExited("Codex App Server closed its stdin") from error

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        request_id = self.new_id()
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.next_message(deadline, f"{method} response")
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result") or {}

    def next_message(self, deadline: float, waiting_for: str) -> dict[str, Any]:
        """Next notification or response. Server requests are answered here, never returned."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {waiting_for}")
            try:
                message = self.inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if message is None:
                raise CodexExited(f"Codex App Server exited while waiting for {waiting_for}")
            self.show(message)
            if "method" in message and "id" in message:
                self.answer_server_request(message)
                continue
            return message

    def answer_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if method not in APPROVALS:
            self.send({"id": message["id"], "error": {
                "code": -32601, "message": f"{method} is not supported by the agent-history recorder"}})
            return
        approve, deny = APPROVALS[method]
        if self.approvals == "ask":
            params = json.dumps(message.get("params"), ensure_ascii=False)
            note(f"APPROVAL {method}: {params[:600]}")
            print("[recorder] approve? [y/N] ", end="", file=sys.stderr, flush=True)
            approved = sys.stdin.readline().strip().lower() in {"y", "yes"}
        else:
            approved = self.approvals == "accept"
        note(f"answering {method}: {approve if approved else deny}")
        self.send({"id": message["id"], "result": {"decision": approve if approved else deny}})

    def show(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is None:
            error = message.get("error")
            note(f"response id={message.get('id')}" + (f" ERROR {error}" if error else ""))
            return
        # Streaming deltas are recorded but not echoed; the full text arrives on item/completed.
        if method.rsplit("/", 1)[-1].lower().endswith("delta"):
            self.suppressed_deltas += 1
            return
        params = message.get("params") or {}
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            note(f"{method}: {item.get('type', '?')} {describe_item(item, method == 'item/completed')}".rstrip())
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            error = (turn.get("error") or {}).get("message")
            note(f"turn/completed: status={turn.get('status')} ({self.suppressed_deltas} deltas)"
                 + (f" error={error}" if error else ""))
            self.suppressed_deltas = 0
        elif method == "error":
            error = params.get("error") or {}
            note(f"error: {error.get('message')} willRetry={params.get('willRetry')}")
        else:
            note(method)

    def close(self) -> int | None:
        try:
            assert self.process.stdin
            self.process.stdin.close()
        except OSError:
            pass
        # EOF on stdin asks the server to exit; escalate to its whole process group if it doesn't.
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
        for reader in self._readers:
            reader.join(timeout=2)
        return self.process.returncode


def describe_item(item: dict[str, Any], completed: bool) -> str:
    kind = item.get("type")
    if kind == "commandExecution":
        if completed:
            return (f"{item.get('status')} exit={item.get('exitCode')} "
                    f"{item.get('durationMs')}ms: {item.get('command')}")
        return str(item.get("command"))
    if kind == "fileChange":
        changes = item.get("changes") or []
        return ", ".join(f"{(c.get('kind') or {}).get('type')} {c.get('path')}" for c in changes)
    if kind == "agentMessage" and completed:
        text = (item.get("text") or "").replace("\n", " ")
        return f"[{item.get('phase')}] {text[:200]}"
    if kind in {"mcpToolCall", "dynamicToolCall", "collabAgentToolCall"}:
        return f"{item.get('server') or item.get('namespace') or ''}/{item.get('tool')} status={item.get('status')}"
    if kind == "webSearch":
        return str(item.get("query"))
    return ""


def prompts(args: argparse.Namespace) -> Iterator[str]:
    yield from args.prompt
    if args.interactive:
        while True:
            print("[recorder] next prompt (empty line to finish)> ", end="", file=sys.stderr, flush=True)
            line = sys.stdin.readline().strip()
            if not line:
                return
            yield line


def run_turn(server: AppServer, capture: Capture, thread_id: str, prompt: str,
             args: argparse.Namespace) -> dict[str, Any]:
    record: dict[str, Any] = {"prompt": prompt, "turn_id": None, "status": None,
                              "started_at": now(), "ended_at": None}
    capture.meta["turns"].append(record)
    capture.save_meta()
    result = server.request("turn/start", {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
    }, args.rpc_timeout)
    turn_id = record["turn_id"] = result["turn"]["id"]
    capture.save_meta()
    note(f"thread={thread_id} turn={turn_id}")

    deadline = time.monotonic() + args.turn_timeout
    interrupting = False
    while True:
        try:
            message = server.next_message(deadline, "turn/completed")
        except KeyboardInterrupt:
            if interrupting:
                raise
            interrupting = True
            note("Ctrl-C: sending turn/interrupt; press Ctrl-C again to abort immediately")
            server.send({"id": server.new_id(), "method": "turn/interrupt",
                         "params": {"threadId": thread_id, "turnId": turn_id}})
            deadline = min(deadline, time.monotonic() + 30)
            continue
        turn = (message.get("params") or {}).get("turn") or {}
        if message.get("method") == "turn/completed" and turn.get("id") == turn_id:
            record["status"] = turn.get("status")
            record["ended_at"] = now()
            capture.save_meta()
            return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="*", help="Prompts to run as consecutive turns on one thread.")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="After the given prompts, keep reading follow-up prompts from the terminal.")
    parser.add_argument("--cwd", default=".", help="Workspace Codex works in (default: current directory).")
    parser.add_argument("--captures", default=os.environ.get("AGENT_HISTORY_CAPTURES", str(DEFAULT_CAPTURES)),
                        help="Directory that receives one sub-directory per session.")
    parser.add_argument("--codex", default=os.environ.get("CODEX_BIN", "codex"), help="Codex CLI executable.")
    parser.add_argument("--model", help="Model to request in thread/start (default: Codex config).")
    parser.add_argument("--sandbox", default="workspace-write",
                        choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--approval-policy", default="never", choices=["untrusted", "on-request", "never"])
    parser.add_argument("--approvals", default="decline", choices=["decline", "accept", "ask"],
                        help="How to answer approval requests Codex sends (default: decline).")
    parser.add_argument("--rpc-timeout", type=float, default=120, help="Seconds to wait for each request's response.")
    parser.add_argument("--turn-timeout", type=float, default=1800, help="Total seconds allowed per turn.")
    args = parser.parse_args()
    if not args.prompt and not args.interactive:
        parser.error("give at least one prompt, or --interactive")
    # Ctrl-C drives turn/interrupt, so make sure it is live even if our parent ignored SIGINT.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        codex_version = subprocess.run([args.codex, "--version"], capture_output=True, text=True,
                                       timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        note(f"cannot run {args.codex}: {error}")
        return 1

    cwd = str(Path(args.cwd).resolve())
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    capture = Capture(Path(args.captures).resolve() / name, {
        "recorder": {"name": "agent-history-recorder", "version": RECORDER_VERSION},
        "codex": {"bin": args.codex, "version": codex_version},
        "settings": {"cwd": cwd, "model": args.model, "sandbox": args.sandbox,
                     "approval_policy": args.approval_policy, "approvals": args.approvals},
        "started_at": now(), "ended_at": None,
        "thread_id": None, "turns": [],
        "outcome": "running", "error": None,
        "codex_exit_code": None, "codex_stderr_tail": [],
    })
    note(f"{codex_version}; recording to {capture.dir}")
    server = AppServer(args.codex, capture, args.approvals)
    meta = capture.meta
    try:
        server.request("initialize", {
            "clientInfo": {"name": "agent-history-recorder", "version": RECORDER_VERSION},
            "capabilities": {"experimentalApi": True},
        }, args.rpc_timeout)
        server.send({"method": "initialized"})
        thread_params: dict[str, Any] = {"cwd": cwd, "approvalPolicy": args.approval_policy,
                                         "sandbox": args.sandbox}
        if args.model:
            thread_params["model"] = args.model
        meta["thread_id"] = server.request("thread/start", thread_params, args.rpc_timeout)["thread"]["id"]
        capture.save_meta()
        for prompt in prompts(args):
            if run_turn(server, capture, meta["thread_id"], prompt, args)["status"] == "interrupted":
                meta["outcome"] = "interrupted"
                break
        else:
            meta["outcome"] = "completed"
    except KeyboardInterrupt:
        meta["outcome"], meta["error"] = "aborted", "aborted by user"
    except CodexExited as error:
        meta["outcome"], meta["error"] = "codex_exited", str(error)
    except TimeoutError as error:
        meta["outcome"], meta["error"] = "timeout", str(error)
    except (RuntimeError, KeyError, TypeError) as error:
        meta["outcome"], meta["error"] = "error", f"{type(error).__name__}: {error}"
    finally:
        meta["codex_exit_code"] = server.close()
        meta["codex_stderr_tail"] = list(server.stderr_tail)
        meta["ended_at"] = now()
        capture.save_meta()
        capture.close()
    note(f"outcome={meta['outcome']}" + (f" ({meta['error']})" if meta["error"] else ""))
    note(f"capture: {capture.dir}")
    return 0 if meta["outcome"] in {"completed", "interrupted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
