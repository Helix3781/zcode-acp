#!/usr/bin/env python3
"""Minimal OpenAI-compatible server that logs every request it receives.

Use it as a "known good" upstream while debugging a ZCode provider: point the
provider's baseUrl at this server (see docs/integrations/aionui-team-mode.md),
run an ACP turn, and look at the log.

    python3 tools/aionui/fake_openai_server.py --port 3925 --log /tmp/fake_llm.log

  * if requests show up  -> the bridge/CLI stack is fine, the real provider is the problem
  * if nothing shows up  -> the turn never reaches the model call (auth, config, or a stall)

Serves both streaming (`"stream": true`) and non-streaming chat completions.
No third-party dependencies; Python 3.9+.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import threading
import time

ARGS: argparse.Namespace
LOG_LOCK = threading.Lock()


def log(*parts: object) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(p) for p in parts))
    with LOG_LOCK:
        print(line, flush=True)
        if ARGS.log:
            with open(ARGS.log, "a") as handle:
                handle.write(line + "\n")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # silence the default access log
        pass

    def _body(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length).decode("utf-8", "replace") if length else ""

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        log("GET", self.path)
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list",
                        "data": [{"id": ARGS.model, "object": "model", "owned_by": "fake"}]})
        else:
            self._json({"object": "list", "data": []})

    def do_POST(self) -> None:
        raw = self._body()
        log("POST", self.path)
        log("  headers:", json.dumps(dict(self.headers), ensure_ascii=False))
        log("  body:", raw[:2000])
        try:
            request = json.loads(raw)
        except Exception:
            request = {}

        model = request.get("model") or ARGS.model
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for delta, finish in (({"role": "assistant", "content": ARGS.reply}, None), ({}, "stop")):
                chunk = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
                         "created": int(time.time()), "model": model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        else:
            self._json({"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": ARGS.reply},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=3925)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default="/tmp/fake_llm.log")
    parser.add_argument("--model", default="fake-glm", help="model id reported by /v1/models")
    parser.add_argument("--reply", default="ok", help="assistant text returned for every request")
    ARGS = parser.parse_args()

    log("=== fake OpenAI-compatible server on http://%s:%d/v1 ===" % (ARGS.host, ARGS.port))
    Server((ARGS.host, ARGS.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
