#!/usr/bin/env python3
"""Sweep every loopback listener for the agent RPC server.

The bundled wecom.exe is a thin client for a loopback NDJSON RPC server
(handshake {"type":"hello","token":...} -> {"type":"welcome",...}).
If that server listens persistently, we can drive 大圆 with no UI at all.

Read-only: one connect + one hello per port, short timeouts.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys

PORTS = [
    9882, 9883, 50010, 50018, 53795, 5821,
    53762, 53763, 53764, 53765, 53766, 53767, 53768, 53769,
    53770, 53772, 53773, 53776, 53777, 53780, 53781,
    53783, 53784, 53786, 53787, 53789, 53790, 53792, 53793,
    53796, 53797, 53835, 53836, 53839, 53840, 53842, 53843,
    53845, 53846, 53848, 53849, 53851, 53852, 53856, 53857,
    53859, 53860, 53862, 53863, 53865, 53866, 53869, 53870,
    53913, 53914, 55504, 55505, 53900, 53901, 53903,
    8326, 19387, 51547, 33173, 59232, 60258, 7897, 5040, 42050,
]


def owner(port: int) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -State Listen -LocalPort {port} "
             "-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if not out:
            return "?"
        name = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {out} -ErrorAction SilentlyContinue).ProcessName"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return f"{out}/{name}"
    except Exception:
        return "?"


def try_hello(port: int) -> str | None:
    """Send the documented RPC hello; report anything that answers."""
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return None
    try:
        s.sendall(b'{"type":"hello","token":""}\n')
        data = s.recv(4096)
        if data:
            return data[:400].decode("utf-8", "replace")
        return "<closed>"
    except socket.timeout:
        return "<no reply>"
    except OSError as exc:
        return f"<err {exc}>"
    finally:
        s.close()


def try_http(port: int) -> str | None:
    """A GET / probe to classify HTTP-ish ports."""
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return None
    try:
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        data = s.recv(2048)
        if data:
            return data[:300].decode("utf-8", "replace").replace("\r\n", " | ")
        return "<closed>"
    except socket.timeout:
        return "<no reply>"
    except OSError as exc:
        return f"<err {exc}>"
    finally:
        s.close()


def main() -> None:
    print(f"{'port':>6}  {'owner':<22} {'rpc-hello':<42} http")
    print("-" * 110)
    for p in PORTS:
        who = owner(p)
        hello = try_hello(p)
        if hello is None:
            print(f"{p:>6}  {who:<22} {'<not listening>':<42}")
            continue
        http = try_http(p)
        print(f"{p:>6}  {who:<22} {str(hello)[:42]:<42} {str(http)[:60]}")


if __name__ == "__main__":
    sys.exit(main())
