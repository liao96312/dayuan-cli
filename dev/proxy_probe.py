#!/usr/bin/env python3
"""Can we reach the 大圆 cloud through the local proxy?

WXWork reaches Tencent over a proxy route; our direct dial to
<cloud-host>:443 times out. Clash (verge-mihomo) listens on
127.0.0.1:7897. If the cloud CGI answers through it, we can drive the
real CLI ourselves.

    python dev/proxy_probe.py
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys

HOST = os.environ.get("DAYUAN_CLOUD_HOST", "<cloud-host>")
PATH = "/cgi-bin/claw/mcp/get"
PROXIES = [None, ("127.0.0.1", 7897), ("127.0.0.1", 7890), ("127.0.0.1", 10809)]
BODY = json.dumps({"limit": 10, "type": "all"}).encode()


def via_proxy(proxy: tuple[str, int] | None, timeout: float = 15.0) -> str:
    try:
        if proxy is None:
            raw = socket.create_connection((HOST, 443), timeout=timeout)
        else:
            raw = socket.create_connection(proxy, timeout=timeout)
            # HTTP CONNECT tunnel
            raw.sendall(
                f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n"
                "Proxy-Connection: keep-alive\r\n\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                d = raw.recv(4096)
                if not d:
                    return "proxy closed during CONNECT"
                resp += d
            if b" 200 " not in resp.split(b"\r\n")[0]:
                return f"CONNECT refused: {resp.split(chr(13).encode())[0]!r}"
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(raw, server_hostname=HOST) as ss:
            head = (
                f"POST {PATH} HTTP/1.1\r\nHost: {HOST}\r\n"
                "Content-Type: application/json\r\n"
                "x-wecom-role: default\r\n"
                f"Content-Length: {len(BODY)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            ss.sendall(head + BODY)
            data = b""
            while len(data) < 6000:
                try:
                    d = ss.recv(4096)
                except socket.timeout:
                    break
                if not d:
                    break
                data += d
        return f"{len(data)} bytes: {data[:700].decode('utf-8','replace')!r}"
    except Exception as exc:  # noqa: BLE001
        return f"FAILED {exc!r}"


def main() -> int:
    for p in PROXIES:
        label = "direct" if p is None else f"proxy {p[0]}:{p[1]}"
        print(f"=== {label} ===")
        print("  ", via_proxy(p)[:900])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
