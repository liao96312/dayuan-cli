#!/usr/bin/env python3
"""Probe the \\\\.\\pipe\\yybclaw IPC pipe and wxwork:// URL schemes.

'claw' is 大圆's internal name (WeClaw). A named pipe called yybclaw is
therefore a strong candidate for a non-UI local control surface: talking
to it needs no window, no focus, no OCR.

Read-only in spirit: we open the pipe, try a couple of benign handshake
shapes, and report whatever comes back. Nothing is sent that could
change user data.

    python dev/pipe_probe.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.ReadFile.restype = wt.BOOL
k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                         ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.WriteFile.restype = wt.BOOL
k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                          ctypes.POINTER(wt.DWORD), ctypes.c_void_p]

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

PIPES = [r"\\.\pipe\yybclaw"]


def try_pipe(path: str) -> None:
    print(f"\n=== {path} ===")
    for access, label in ((GENERIC_READ | GENERIC_WRITE, "RW"),
                          (GENERIC_READ, "R"),
                          (GENERIC_WRITE, "W")):
        h = k32.CreateFileW(path, access, 0, None, OPEN_EXISTING, 0, None)
        if h == wt.HANDLE(-1).value or not h:
            err = ctypes.get_last_error()
            print(f"  open {label}: failed err={err}")
            continue
        print(f"  open {label}: OK handle={h}")
        # Try to read anything already queued.
        buf = ctypes.create_string_buffer(8192)
        n = wt.DWORD(0)
        if k32.ReadFile(h, buf, 8192, ctypes.byref(n), None) and n.value:
            print(f"    read {n.value} bytes: {buf.raw[:400]!r}")
        else:
            print(f"    read: nothing (err={ctypes.get_last_error()})")
        # Try a couple of benign probe writes.
        for probe in (b'{"type":"ping"}\n', b'ping\n'):
            w = wt.DWORD(0)
            ok = k32.WriteFile(h, probe, len(probe), ctypes.byref(w), None)
            print(f"    write {probe!r}: ok={bool(ok)} written={w.value} "
                  f"err={ctypes.get_last_error()}")
            if ok:
                n2 = wt.DWORD(0)
                if k32.ReadFile(h, buf, 8192, ctypes.byref(n2), None) and n2.value:
                    print(f"      reply {n2.value}B: {buf.raw[:600]!r}")
                    break
        k32.CloseHandle(h)
        break


def pipe_instances(name: str) -> int:
    """Count instances of a pipe by opening until failure."""
    count = 0
    handles = []
    for _ in range(64):
        h = k32.CreateFileW(rf"\\.\pipe\{name}", GENERIC_READ | GENERIC_WRITE,
                            0, None, OPEN_EXISTING, 0, None)
        if not h or h == wt.HANDLE(-1).value:
            break
        handles.append(h)
        count += 1
    for h in handles:
        k32.CloseHandle(h)
    return count


def list_claw_pipes() -> None:
    print("=== pipes mentioning claw / wecom / agent ===")
    try:
        names = os.listdir(r"\\.\pipe\\")
    except OSError as exc:
        print("  listdir failed:", exc)
        return
    for n in sorted(names):
        low = n.lower()
        if any(k in low for k in ("claw", "wecom", "agent", "wxwork", "dayuan")):
            inst = pipe_instances(n) if "claw" in low else "?"
            print(f"  {n}   instances={inst}")


def main() -> int:
    list_claw_pipes()
    for p in PIPES:
        try_pipe(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
