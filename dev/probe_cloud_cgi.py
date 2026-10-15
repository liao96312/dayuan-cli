#!/usr/bin/env python3
"""Test the new cloud endpoint the engine actually uses.

The 1.3.0 engine ignores local 9882/9883 and calls
https://<cloud-host>/cgi-bin/claw/... directly, logging
has_authorization=false. This probes whether that CGI answers an
unauthenticated caller, and with which status.
"""
from __future__ import annotations

import json
import os
import socket
import ssl

HOST = os.environ.get("DAYUAN_CLOUD_HOST", "<cloud-host>")
PATHS = [
    "/cgi-bin/claw/agent_resource/get_agent_resource_effective_list",
    "/cgi-bin/claw/mcp/get",
    "/cgi-bin/claw/openclaw/get_user_initialization_info",
]


def post(path: str, body: dict, extra: dict[str, str], timeout: float = 12.0) -> None:
    payload = json.dumps(body).encode()
    headers = {
        "Host": HOST,
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
        "User-Agent": "1.3.0/26166a5 (2026-10-13 21:33:22 +08:00) [windows/x86]",
        "Connection": "close",
    }
    headers.update(extra)
    raw = (f"POST {path} HTTP/1.1\r\n" +
           "\r\n".join(f"{k}: {v}" for k, v in headers.items()) +
           "\r\n\r\n").encode() + payload
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((HOST, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as ss:
                ss.sendall(raw)
                data = b""
                while len(data) < 4000:
                    try:
                        chunk = ss.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    data += chunk
    except Exception as exc:  # noqa: BLE001
        print(f"  {path}\n    FAILED: {exc!r}")
        return
    print(f"  {path}\n    {len(data)} bytes: "
          f"{data[:500].decode('utf-8','replace').replace(chr(13)+chr(10), ' | ')}")


def main() -> None:
    print("=== unauthenticated (mimics engine's has_authorization=false) ===")
    for p in PATHS:
        post(p, {"limit": 10, "type": "all"}, {})

    print("\n=== with x-openclaw-approvalid (engine sends this) ===")
    for p in PATHS:
        post(p, {"limit": 10, "type": "all"},
             {"x-wecom-role": "default",
              "x-openclaw-approvalid": "01a13d2b-15a9-7783-b35a-7aa9c791df38"})


if __name__ == "__main__":
    main()
