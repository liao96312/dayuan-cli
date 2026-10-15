#!/usr/bin/env python3
"""Probe the browser MCP channel on 53795 as WebSocket / SSE.

The live engine holds two ESTABLISHED connections to 127.0.0.1:53795
(browser_mcp_url = http://127.0.0.1:53795/mcp/cef/v1). That endpoint
drives WXWork's embedded CEF browser -- i.e. the 大圆 chat surface -- via
a machine protocol rather than window automation.

HTTP probes got no reply, so try the streaming transports MCP uses.

    python dev/browser_mcp_probe.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import time

HOST = "127.0.0.1"
PORT = 53795
PATHS = ["/mcp/cef/v1", "/mcp", "/"]


def ws_handshake(sock: socket.socket, path: str) -> bool:
    key = base64.b64encode(b"dayuan-cli-probe-0123").decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: mcp\r\n\r\n"
    ).encode()
    sock.sendall(req)
    data = b""
    sock.settimeout(6)
    try:
        while b"\r\n\r\n" not in data:
            d = sock.recv(4096)
            if not d:
                break
            data += d
    except socket.timeout:
        return False
    return b"101" in data.split(b"\r\n")[0]


def ws_frame(payload: str) -> bytes:
    data = payload.encode()
    mask = b"dayuan"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytes([0x81])  # FIN + text
    n = len(data)
    if n < 126:
        header += bytes([0x80 | n])
    else:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    return header + masked


def ws_read(sock: socket.socket) -> str | None:
    try:
        head = sock.recv(2)
        if len(head) < 2:
            return None
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", sock.recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", sock.recv(8))[0]
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                break
            out += chunk
        return out.decode("utf-8", "replace")
    except socket.timeout:
        return None
    except OSError:
        return None


def try_ws(path: str) -> None:
    print(f"\n=== WS {path} ===")
    s = socket.socket()
    s.settimeout(6)
    try:
        s.connect((HOST, PORT))
    except OSError as exc:
        print(f"  connect failed: {exc}")
        return
    try:
        if not ws_handshake(s, path):
            print("  no 101 upgrade")
            s.close()
            return
        print("  upgraded -> sending initialize")
        s.sendall(ws_frame(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "dayuan-cli", "version": "1"}}})))
        for _ in range(3):
            r = ws_read(s)
            if r:
                print(f"  <- {r[:800]}")
                break
            print("  (no reply)")
        s.sendall(ws_frame(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized"})))
        s.sendall(ws_frame(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})))
        for _ in range(3):
            r = ws_read(s)
            if r:
                print(f"  <- {r[:1500]}")
                break
    except OSError as exc:
        print(f"  error: {exc}")
    finally:
        s.close()


def try_sse(path: str) -> None:
    print(f"\n=== SSE {path} ===")
    s = socket.socket()
    s.settimeout(6)
    try:
        s.connect((HOST, PORT))
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
                   "Accept: text/event-stream\r\n"
                   "Connection: keep-alive\r\n\r\n").encode())
        data = s.recv(4096)
        print(f"  {len(data)} bytes: {data[:600]!r}")
    except (OSError, socket.timeout) as exc:
        print(f"  error: {exc!r}")
    finally:
        s.close()


def main() -> int:
    for p in PATHS:
        try_sse(p)
    for p in PATHS:
        try_ws(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
