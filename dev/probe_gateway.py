#!/usr/bin/env python3
"""Probe the live WeCom gateways to see what an external process can reach.

Read-only: connects, sends one request, prints the reply. Used to decide
whether a non-UI path can ride the real cloud LLM (9883) or MCP (9882).
"""
from __future__ import annotations

import json
import os
import socket
import tempfile


def probe(port: int, payload: bytes, label: str, timeout: float = 6.0) -> None:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
    except OSError as exc:
        print(f"[{label}:{port}] connect failed: {exc}")
        return
    if payload:
        try:
            s.sendall(payload)
        except OSError as exc:
            print(f"[{label}:{port}] send failed: {exc}")
            s.close()
            return
    try:
        data = s.recv(8192)
    except socket.timeout:
        print(f"[{label}:{port}] connected, NO reply within {timeout}s")
    except OSError as exc:
        print(f"[{label}:{port}] recv failed: {exc}")
    else:
        if not data:
            print(f"[{label}:{port}] connected then closed (0 bytes)")
        else:
            print(f"[{label}:{port}] {len(data)} bytes: {data[:800]!r}")
    s.close()


def http_get(path: str, host: str = "127.0.0.1", extra: str = "") -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\n{extra}"
        "User-Agent: probe\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    ).encode()


def http_post(path: str, body: bytes, host: str = "127.0.0.1", extra: str = "") -> bytes:
    head = (
        f"POST {path} HTTP/1.1\r\nHost: {host}\r\n{extra}"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Accept: */*\r\nConnection: close\r\n\r\n"
    ).encode()
    return head + body


def main() -> None:
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "probe", "version": "1"}},
    }).encode()

    print("=== 9883 LLM gateway (real cloud proxy) ===")
    probe(9883, http_get("/v1/models"), "llm-noauth")
    probe(9883, http_get("/v1/models", extra="Authorization: Bearer sk-test\r\n"), "llm-bearer")

    print("\n=== 9882 MCP gateway ===")
    probe(9882, http_post("/mcp", init), "mcp-init")

    print("\n=== control channels ===")
    probe(50018, b"", "ws-50018", timeout=4)
    probe(50010, b"", "p-50010", timeout=4)

    print("\n=== temp entries mentioning wecom/agent/claw ===")
    tmp = tempfile.gettempdir()
    try:
        for name in sorted(os.listdir(tmp)):
            low = name.lower()
            if any(k in low for k in ("wecom", "agent", "claw", "wxwork", "dayuan")):
                full = os.path.join(tmp, name)
                print("  ", full, "(dir)" if os.path.isdir(full) else "(file)")
    except OSError as exc:
        print("  temp listing failed:", exc)


if __name__ == "__main__":
    main()
