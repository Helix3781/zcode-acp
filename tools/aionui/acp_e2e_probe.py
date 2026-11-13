#!/usr/bin/env python3
"""End-to-end ACP probe for the zcode-acp-server bridge.

Speaks Agent Client Protocol over stdio to any ACP agent command and runs one
turn, printing every update it receives. Handy for separating "the bridge is
broken" from "the upstream provider is broken":

    # default: probe the installed bridge, select the model from provider_config
    python3 tools/aionui/acp_e2e_probe.py

    # explicit command + env, pick a specific model, dump bridge logs to a file
    python3 tools/aionui/acp_e2e_probe.py \
        --command /opt/homebrew/bin/zcode-acp-server \
        --env ZCODE_BIN=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
        --env ZCODE_ACP_MODE=yolo \
        --model 'new-provider\\glm-5.3-flash' \
        --log /tmp/bridge.err

Exit codes:
  0  the turn completed (a session/prompt response arrived)
  1  the turn failed (protocol error or turn_failed update)
  2  the probe timed out

No third-party dependencies; Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

DEFAULT_COMMAND = ["zcode-acp-server"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--command", default=os.environ.get("PROBE_COMMAND", DEFAULT_COMMAND[0]),
                   help="ACP agent command to spawn (default: %(default)s)")
    p.add_argument("--arg", action="append", default=[], help="extra argv entry for the command (repeatable)")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="extra environment variable for the agent (repeatable)")
    p.add_argument("--cwd", default=os.getcwd(), help="session cwd (default: current directory)")
    p.add_argument("--prompt", default="Reply with exactly: ok", help="prompt text to send")
    p.add_argument("--model", default=None,
                   help="value for the `model` config option, e.g. 'new-provider\\\\glm-5.3-flash'")
    p.add_argument("--timeout", type=float, default=180.0, help="hard deadline in seconds (default: %(default)s)")
    p.add_argument("--log", default=None, help="file to capture the agent's stderr (default: a temp file)")
    p.add_argument("--quiet", action="store_true", help="only print the final verdict")
    return p.parse_args()


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.t0 = time.time()
        self.pending: dict[int, str] = {}
        self.session_id: str | None = None
        self.result: str | None = None
        env = dict(os.environ)
        for item in args.env:
            key, _, value = item.partition("=")
            env[key] = value
        self.env = env

    # ---- output helpers -------------------------------------------------
    def log(self, *parts: object) -> None:
        if not self.args.quiet:
            print("[%6.1fs]" % (time.time() - self.t0), *parts, flush=True)

    def chunk(self, text: str) -> None:
        if not self.args.quiet:
            print(text, end="", flush=True)

    # ---- protocol -------------------------------------------------------
    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, message_id: int, method: str, params: dict) -> None:
        self.pending[message_id] = method
        self.send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})

    def handle(self, message: dict) -> None:
        if "method" in message and "id" not in message:
            method = message["method"]
            if method == "session/update":
                update = message.get("params", {}).get("update", {})
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    content = update.get("content", {})
                    if content.get("type") == "text":
                        self.chunk(content["text"])
                elif kind in ("turn_failed", "turn_end", "session_end"):
                    self.log("UPDATE", json.dumps(update, ensure_ascii=False)[:400])
                    if kind == "turn_failed":
                        self.result = "failed"
                return
            # notifications such as $/zcode/turnState — harmless, just log them
            self.log("NOTIFY", method)
            return

        if "id" not in message:
            return

        method = self.pending.pop(message["id"], "?")
        self.log("RESP", method, json.dumps(message, ensure_ascii=False)[:400])

        if method == "initialize":
            self.request(2, "session/new", {"cwd": self.args.cwd, "mcpServers": []})
        elif method == "session/new":
            if "error" in message:
                self.result = "failed"
                return
            self.session_id = message["result"]["sessionId"]
            if self.args.model:
                self.request(5, "session/set_config_option",
                             {"sessionId": self.session_id, "configId": "model", "value": self.args.model})
            else:
                self.prompt()
        elif method == "session/set_config_option":
            self.prompt()
        elif method == "session/prompt":
            if "error" in message:
                self.result = "failed"
            else:
                self.result = "ok"
                self.log("PROMPT DONE stopReason=", message["result"].get("stopReason"))

    def prompt(self) -> None:
        assert self.session_id is not None
        self.request(3, "session/prompt",
                     {"sessionId": self.session_id,
                      "prompt": [{"type": "text", "text": self.args.prompt}]})

    # ---- lifecycle ------------------------------------------------------
    def run(self) -> int:
        log_path = self.args.log or "/tmp/acp_e2e_probe.err"
        log_file = open(log_path, "w")
        argv = [self.args.command, *self.args.arg]
        if not self.args.quiet:
            print("command :", " ".join(argv))
            print("stderr  :", log_path)
        self.proc = subprocess.Popen(argv, cwd=self.args.cwd, env=self.env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=log_file, text=True, bufsize=1)

        def reader() -> None:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.handle(json.loads(line))
                except Exception:
                    self.log("NON-JSON", line[:200])

        threading.Thread(target=reader, daemon=True).start()

        self.request(1, "initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            "clientInfo": {"name": "zcode-acp-acp-probe", "version": "1"},
        })

        deadline = time.time() + self.args.timeout
        while time.time() < deadline and self.result is None and self.proc.poll() is None:
            time.sleep(0.5)

        if self.proc.poll() is not None and self.result is None:
            self.log("agent exited with rc=", self.proc.returncode)
        if self.result is None:
            self.log("TIMEOUT after %.0fs" % self.args.timeout)
        self.proc.kill()
        log_file.flush()

        if self.result is None:
            verdict = 2
        elif self.result == "ok":
            verdict = 0
        else:
            verdict = 1
        if not self.args.quiet:
            print("\n=== verdict:", {0: "OK (turn completed)", 1: "FAILED (turn error)",
                                     2: "TIMEOUT (no session/prompt response)"}[verdict])
            tail = open(log_path).read()[-2000:]
            if tail.strip():
                print("=== agent stderr (tail) ===")
                print(tail)
        return verdict


def main() -> int:
    return Probe(parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
