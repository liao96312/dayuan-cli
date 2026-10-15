#!/usr/bin/env python3
"""Dump a large window of live-engine memory around a byte offset.

Read-only. Used to inspect the resolved provider config (api_key) and the
TokensConfig the engine received over the control channel.

    python dev/mem_dump.py --pid 14028 --base 0x44b0000 --off 854206 --len 6000
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def find_pid(name: str) -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process {name} -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    return int(out) if out.isdigit() else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--name", default="wecom-agent")
    ap.add_argument("--base", type=lambda s: int(s, 0), required=True)
    ap.add_argument("--off", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--len", type=int, default=4000)
    args = ap.parse_args()

    pid = args.pid or find_pid(args.name)
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        return 3

    addr = args.base + args.off
    buf = ctypes.create_string_buffer(args.len)
    n = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, args.len, ctypes.byref(n))
    k32.CloseHandle(h)
    if not ok:
        print(f"read failed at 0x{addr:x}")
        return 4

    data = buf.raw[: n.value]
    txt = "".join(chr(c) if 32 <= c < 127 else ("\n" if c in (10, 13) else ".") for c in data)
    print(f"--- 0x{addr:x} ({n.value} bytes) ---")
    print(txt)

    print("\n--- token-shaped strings (>=32) ---")
    for m in re.finditer(rb"[A-Za-z0-9_\-\.=+/]{32,300}", data):
        s = m.group().decode()
        if not re.fullmatch(r"[a-z_]+", s):
            print(f"  {s[:30]}...{s[-10:]} (len={len(s)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
