#!/usr/bin/env python3
"""Replay the captured wecom CLI MCP request against the real gateway.

The capture showed the CLI's wire format:

  POST /mcp  {"jsonrpc":"2.0","id":1,"method":"tools/call",
              "params":{"name":"wecom_tool",
                        "arguments":{"payload":"{\"headers_json\":...,
                                     \"arguments_json\":\"[\\"contact\\",...]\"}"}}}

If 9882 answers this, we can drive the real 大圆 CLI with no engine and
no UI at all.

    python dev/gateway_replay.py
"""
from __future__ import annotations

import json
import socket
import sys
import uuid

GATEWAYS = [("127.0.0.1", 9882, "/mcp"), ("127.0.0.1", 53795, "/mcp/cef/v1")]


def make_payload(argv: list[str]) -> str:
    headers = {
        "x-wecom-role": "default",
        "x-agent-info": json.dumps({
            "user_agent": "1.3.0/26166a5 (2026-10-13 21:33:22 +08:00) [windows/x86]"}),
        "x-openclaw-approvalid": str(uuid.uuid4()),
    }
    return json.dumps({
        "headers_json": json.dumps(headers, ensure_ascii=False),
        "arguments_json": json.dumps(argv, ensure_ascii=False),
    }, ensure_ascii=False)


def body_for(argv: list[str]) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "wecom_tool",
                   "arguments": {"payload": make_payload(argv)}},
    }, ensure_ascii=False).encode()


def post(host: str, port: int, path: str, body: bytes, timeout: float = 20.0) -> None:
    head = (
        f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        "Accept: */*\r\n"
        "Accept-Encoding: identity\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    s = socket.socket()
    s.settimeout(timeout)
    label = f"{host}:{port}{path}"
    try:
        s.connect((host, port))
    except OSError as exc:
        print(f"[{label}] connect failed: {exc}")
        return
    try:
        s.sendall(head + body)
        chunks = []
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                chunks.append(b"<TIMEOUT>")
                break
            if not d:
                break
            chunks.append(d)
            if sum(len(c) for c in chunks) > 300000:
                break
        data = b"".join(chunks)
        print(f"[{label}] {len(data)} bytes")
        print("   ", data[:1800].decode("utf-8", "replace").replace("\r\n", " | "))
    except OSError as exc:
        print(f"[{label}] send/recv failed: {exc}")
    finally:
        s.close()


def main() -> int:
    argv = sys.argv[1:] or ["contact", "users", "search", "--json",
                            '{"keywords": ["张三"]}']
    print(f"argv = {argv}\n")
    body = body_for(argv)

    for host, port, path in GATEWAYS:
        print(f"=== {host}:{port}{path} ===")
        post(host, port, path, body)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
