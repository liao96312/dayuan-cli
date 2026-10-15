#!/usr/bin/env python3
"""Attempt to send a message via the CLI, riding the engine's own cloud route.

Diagnosis so far:
  * Our engine authenticates and reaches the cloud CLI (we see real
    CGI requests with trust_dev_info and a resolved device_id).
  * The only failing hop is mcp://service.discovery, which the CLI asks
    for on its first call, and which the real 9882 gateway refuses for
    a non-WXWork-owned engine.

So: answer service.discovery correctly and the CLI should complete.

This replays the captured request shape, but routes through the LOCAL
gateway 9882 from inside our engine (which holds the Tencent route).

    python dev/send_try.py --to 张三 --text "..."
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shim_drive import LlmShim, SHIM_PORT, launch_engine, run_session  # noqa: E402
import shim_drive

STATE = r"D:\dayuan-cli\state"
PROMPT_LOG = os.path.join(STATE, "dayuan-ws.log")


def log(m: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    try:
        with open(PROMPT_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class ShimBase(LlmShim):
    """Same as the driver shim; kept separate for a distinct log."""


class McpProxy(BaseHTTPRequestHandler):
    """Forward MCP requests to the real gateway, log everything."""

    protocol_version = "HTTP/1.1"
    CAP = os.path.join(STATE, "mcp-capture2.jsonl")

    def _log(self, body: bytes, resp: bytes) -> None:
        try:
            with open(self.CAP, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(), "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()
                                if k.lower() not in ("host", "content-length")},
                    "body": body.decode("utf-8", "replace")[:4000],
                    "resp": resp[:2000].decode("utf-8", "replace"),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        # Try to extract argv from the captured payload shape.
        argv = None
        try:
            req = json.loads(raw.decode("utf-8"))
            params = (req.get("params") or {})
            args = params.get("arguments") or {}
            payload = args.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
                argv = json.loads(payload.get("arguments_json", "[]"))
        except Exception:  # noqa: BLE001
            argv = None
        log(f"MCP tools/call argv={argv}")

        # Forward to the real gateway on the same path.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", 9882, timeout=30)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection",
                                        "accept-encoding")}
        out = b""
        try:
            conn.request("POST", self.path, body=raw, headers=headers)
            resp = conn.getresponse()
            out = resp.read()
            log(f"MCP gateway -> {resp.status} {out[:300]!r}")
        except Exception as exc:  # noqa: BLE001
            out = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32000, "message": f"proxy failed: {exc!r}"}}).encode()
            log(f"MCP proxy exception: {exc!r}")
        finally:
            conn.close()
        self._log(raw, out)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802
        b = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *_a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="张三")
    ap.add_argument("--text", default="这是来自 dayuan-cli 非 UI 通道的测试消息。")
    ap.add_argument("--timeout", type=int, default=100)
    args = ap.parse_args()

    shim_drive.SCRIPT = shim_drive.Script([
        {"kind": "tool", "name": "exec",
         "args": {
             "command": "wecom contact users search "
                        "--json '{\"keywords\": [\"张三\"]}'",
             "execution_purpose": "查询联系人张三的信息"}},
        {"kind": "text", "text": "SEARCH-DONE"},
    ])

    shim = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), LlmShim)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    proxy = ThreadingHTTPServer(("127.0.0.1", 19892), McpProxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    log("shim + mcp-proxy up")

    proc, fh = launch_engine("http://127.0.0.1:19892/mcp")
    try:
        state = asyncio.run(run_session("查一下联系人张三", args.timeout))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()
        shim.shutdown()
        proxy.shutdown()

    print("\n=== captured MCP traffic ===")
    try:
        with open(os.path.join(STATE, "mcp-capture2.jsonl"), encoding="utf-8") as f2:
            for i, line in enumerate(f2, 1):
                rec = json.loads(line)
                print(f"\n--- #{i} {rec['path']} ---")
                print("req :", rec["body"][:600])
                print("resp:", rec["resp"][:600])
    except OSError as exc:
        print("no capture:", exc)
    print("\ntext:", state["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
