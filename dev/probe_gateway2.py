#!/usr/bin/env python3
"""Probe 9883/9882 with the exact headers the engine sends.

Earlier raw-socket probes were reset. The engine sends x-agent-* headers
and (for MCP) x-openclaw-approvalid. This reproduces that shape so we can
tell "wrong client" from "wrong request".
"""
from __future__ import annotations

import json
import socket


def http(port: int, method: str, path: str, body: bytes | None, headers: dict[str, str],
         timeout: float = 8.0) -> None:
    head = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    head += [f"{k}: {v}" for k, v in headers.items()]
    if body is not None:
        head.append(f"Content-Length: {len(body)}")
    head.append("Connection: close")
    raw = ("\r\n".join(head) + "\r\n\r\n").encode() + (body or b"")
    label = f"{method} {path}"
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
    except OSError as exc:
        print(f"[{port} {label}] connect failed: {exc}")
        return
    try:
        s.sendall(raw)
        chunks = []
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                chunks.append(b"<timeout>")
                break
            if not d:
                break
            chunks.append(d)
            if sum(len(c) for c in chunks) > 200000:
                break
        data = b"".join(chunks)
        print(f"[{port} {label}] {len(data)} bytes")
        print("   ", data[:1200].decode("utf-8", "replace").replace("\r\n", " | "))
    except OSError as exc:
        print(f"[{port} {label}] send/recv failed: {exc}")
    finally:
        s.close()


AGENT_INFO = json.dumps({
    "user_agent": "1.3.0/26166a5 (2026-10-13 21:33:22 +08:00) [windows/x86]"
})


def main() -> None:
    llm_headers = {
        "Content-Type": "application/json",
        "x-agent-call-kind": "main_loop",
        "x-trace-id": "trace-probe0001",
        "x-session-id": "conv-probe",
        "x-agent-role": "default",
        "x-agent-turn": "1",
        "x-agent-source": "local_wecomagent",
        "x-agent-info": AGENT_INFO,
    }
    payload = json.dumps({
        "model": "MiniMax-M2.5-WW-128K-Test",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }).encode()

    print("=== 9883 /v1/models with engine headers ===")
    http(9883, "GET", "/v1/models", None, llm_headers)
    print("\n=== 9883 /v1/chat/completions (engine headers, stream) ===")
    http(9883, "POST", "/v1/chat/completions", payload, llm_headers)

    print("\n=== 9882 /mcp with openclaw approvalid ===")
    mcp_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "x-agent-info": AGENT_INFO,
        "x-wecom-role": "default",
        "x-openclaw-approvalid": "01a13d2b-15a9-7783-b35a-7aa9c791df38",
    }
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "probe", "version": "1"}},
    }).encode()
    http(9882, "POST", "/mcp", init, mcp_headers)

    print("\n=== 53795 browser MCP ===")
    http(53795, "POST", "/mcp/cef/v1", init, mcp_headers)
    http(53795, "POST", "/mcp", init, mcp_headers)

    print("\n=== 50010 ===")
    http(50010, "GET", "/", None, {})


if __name__ == "__main__":
    main()
