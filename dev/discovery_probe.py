#!/usr/bin/env python3
"""Is the 9882 MCP gateway refusing us, or refusing everyone?

Earlier evidence: our own engine's direct cloud calls DO authenticate
(trust_dev_info sent, device_id resolved, x-openclaw-approvalid minted).
Only mcp://service.discovery on 9882 fails.

Test both directions from ONE process that already holds a working
Tencent route: our engine. If the same request succeeds from our engine
but fails from a bare socket, the gateway keys on something we can copy.

    python dev/discovery_probe.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shim_drive import LlmShim, SHIM_PORT, launch_engine, run_session  # noqa: E402
import shim_drive

STATE = r"D:\dayuan-cli\state"
CAP = os.path.join(STATE, "discovery-probe.jsonl")


class McpProbe(BaseHTTPRequestHandler):
    """Answer service.discovery from local knowledge; log everything."""

    protocol_version = "HTTP/1.1"
    seen = 0

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        McpProbe.seen += 1
        argv = None
        try:
            req = json.loads(raw.decode("utf-8"))
            params = (req.get("params") or {})
            payload = ((params.get("arguments") or {}).get("payload"))
            if isinstance(payload, str):
                argv = json.loads(json.loads(payload).get("arguments_json", "[]"))
        except Exception:  # noqa: BLE001
            argv = None
        print(f"[mcp] #{McpProbe.seen} argv={argv}", file=sys.stderr, flush=True)

        # The CLI's MCP parser wants a CallToolResult envelope:
        #   content[].text  (human-readable payload as JSON text)
        #   structuredContent  (the same table, parsed)
        #   isError: false
        # service.discovery is the first call it makes; answering it
        # correctly is what lets the real CLI reach the cloud.
        if argv and argv[:2] == ["service", "discovery"]:
            # Catalog schema recovered from the binary (ServiceCatalog):
            #   items[] = ServiceSchema { id, description, resources }
            #   resources[] = ServiceResource { id, hidden, methods }
            #   methods[] = MethodSchema { id, description, path }
            # The CLI builds its clap command tree from this catalog, so
            # we only need to describe the services we intend to call.
            def method(mid: str, desc: str, path: str) -> dict:
                return {"id": mid, "description": desc, "path": path,
                        "hidden": False, "remote_doc": None}

            def resource(rid: str, methods: list[dict]) -> dict:
                return {"id": rid, "methods": methods, "hidden": False}

            def service_catalog() -> dict:
                contact = {
                    "id": "contact",
                    "description": "企业微信通讯录",
                    "resources": [
                        resource("users", [
                            method("search", "按关键词搜索人员",
                                   "contact/users/search"),
                            method("get", "按 userid 批量查人员详情",
                                   "contact/users/get"),
                        ]),
                    ],
                }
                message = {
                    "id": "message",
                    "description": "企业微信消息",
                    "resources": [
                        resource("send", [
                            method("execute", "发送消息", "message/send"),
                        ]),
                        resource("unread", [
                            method("get", "获取未读消息", "message/unread/get"),
                        ]),
                        resource("search", [
                            method("execute", "搜索消息", "message/search"),
                        ]),
                    ],
                }
                return {"items": [contact, message], "code": 0, "message": "ok"}

            payload = {"result": service_catalog(), "code": 0, "message": "ok"}
            out = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(payload, ensure_ascii=False)}],
                    "structuredContent": payload,
                    "isError": False,
                }},
                ensure_ascii=False).encode()
        else:
            out = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": "{}"}],
                "structuredContent": {},
                "isError": False}}, ensure_ascii=False).encode()

        try:
            with open(CAP, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "argv": argv,
                                     "answered": "service.discovery"
                                     if argv and argv[:2] == ["service", "discovery"]
                                     else "empty"},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass

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
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    shim_drive.SCRIPT = shim_drive.Script([
        {"kind": "tool", "name": "exec",
         "args": {"command": "wecom contact users search "
                             "--json '{\"keywords\": [\"张三\"]}'",
                  "execution_purpose": "查询联系人张三的信息"}},
        {"kind": "text", "text": "PROBE-DONE"},
    ])

    shim = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), LlmShim)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    mcp = ThreadingHTTPServer(("127.0.0.1", 19893), McpProbe)
    threading.Thread(target=mcp.serve_forever, daemon=True).start()

    try:
        os.remove(CAP)
    except OSError:
        pass

    proc, fh = launch_engine("http://127.0.0.1:19893/mcp")
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
        mcp.shutdown()

    print("\n=== CLI argv sequence seen by the MCP stub ===")
    try:
        with open(CAP, encoding="utf-8") as f2:
            for line in f2:
                print("  ", json.loads(line).get("argv"))
    except OSError:
        print("  none")
    print("\ntext:", state["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
