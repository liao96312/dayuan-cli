#!/usr/bin/env python3
"""Capture what the wecom CLI asks its MCP endpoint for.

With the live launcher env our engine runs the CLI in-process and
authorizes cloud calls. The CLI's first call is mcp://service.discovery,
which our stub answered with a bare 502. This logs the exact request
(method, params, headers) so we can answer it properly and let the CLI
reach the real cloud.

    python dev/mcp_capture.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shim_drive import (LlmShim, SCRIPT, Script, SHIM_PORT,  # noqa: E402
                        launch_engine, run_session)

CAPTURE = r"D:\dayuan-cli\state\mcp-capture.jsonl"
MCP_PORT = 19892


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


class CapturingMcp(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _record(self, body: bytes) -> None:
        try:
            with open(CAPTURE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(),
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": body.decode("utf-8", "replace")[:6000],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        self._record(raw)
        # Answer with an empty JSON-RPC result so the caller proceeds.
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            req = {}
        rid = req.get("id", 1)
        payload = json.dumps({
            "jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": "{}"}], "isError": False},
        }, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._record(b"")
        body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


def main() -> int:
    import shim_drive

    shim_drive.SCRIPT = Script([
        {"kind": "tool", "name": "exec",
         "args": {"command": "wecom contact users search --json '{\"keywords\": [\"张三\"]}'",
                  "execution_purpose": "查询联系人张三的信息"}},
        {"kind": "text", "text": "CAPTURE-DONE"},
    ])

    try:
        os.remove(CAPTURE)
    except OSError:
        pass

    shim = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), LlmShim)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    mcp = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), CapturingMcp)
    threading.Thread(target=mcp.serve_forever, daemon=True).start()
    log(f"shim :{SHIM_PORT}  capturing mcp :{MCP_PORT}")

    proc, fh = launch_engine(f"http://127.0.0.1:{MCP_PORT}/mcp")
    try:
        state = asyncio.run(run_session("帮我查一下联系人 张三", 70))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()
        shim.shutdown()
        mcp.shutdown()

    print("\n=== captured MCP requests ===")
    try:
        with open(CAPTURE, encoding="utf-8") as f2:
            for i, line in enumerate(f2, 1):
                rec = json.loads(line)
                print(f"\n--- #{i} {rec['path']} ---")
                print("headers:", json.dumps(
                    {k: v for k, v in rec["headers"].items()
                     if k.lower() not in ("host", "content-length")}, ensure_ascii=False)[:500])
                print("body:", rec["body"][:1500])
    except OSError as exc:
        print("no capture:", exc)
    print("\nassistant text:", state["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
