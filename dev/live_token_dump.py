#!/usr/bin/env python3
"""Read the live engine's resolved provider api_key / TokensConfig.

The live engine received TokensConfig over the control channel and, per
its own log line "injected llm_access_token into provider api_key", holds
a resolved provider config in memory with the api_key populated.

Read-only. We scan for the api_key field near the model id, printing only
masked values.

    python dev/live_token_dump.py --pid 14028
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

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x20, 0x40}


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]


def rd(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def regions(h):
    addr, mbi = 0, MBI()
    while addr < 0x7FFFFFFF:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base, size = mbi.BaseAddress or 0, mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and not (mbi.Protect & PAGE_GUARD) and size <= (512 << 20)):
            yield base, size
        addr = base + size
        if size == 0:
            break


def find_pid(name: str) -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process {name} -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    return int(out) if out.isdigit() else 0


def mask(s: str) -> str:
    return f"{s[:8]}…{s[-6:]} (len={len(s)})" if len(s) > 20 else f"(len={len(s)})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--name", default="wecom-agent")
    ap.add_argument("--window", type=int, default=900)
    args = ap.parse_args()

    pid = args.pid or find_pid(args.name)
    if not pid:
        print(f"no process {args.name}")
        return 2
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        return 3
    print(f"pid={pid} opened read-only\n")

    # UTF-16 and ASCII variants of the field name.
    needles = [b"api_key",
               "api_key".encode("utf-16-le"),
               b"access_token",
               "access_token".encode("utf-16-le"),
               b"llm_access_token",
               "llm_access_token".encode("utf-16-le")]
    shown = 0
    scanned = 0
    try:
        for base, size in regions(h):
            data = rd(h, base, size)
            if not data:
                continue
            scanned += len(data)
            for needle in needles:
                for m in re.finditer(re.escape(needle), data):
                    # Value follows shortly after the key.
                    s = m.end()
                    e = min(len(data), s + args.window)
                    chunk = data[s:e]
                    # printable ASCII runs
                    for vm in re.finditer(rb"[\x20-\x7e]{24,300}", chunk):
                        v = vm.group().decode()
                        if v.startswith(("api_", "{", "[", "MiniMax", "HY-", "http",
                                         "wecom", "openai")):
                            continue
                        if not re.search(r"[A-Za-z0-9]{20,}", v):
                            continue
                        print(f"[{needle[:20].decode('utf-8','replace')}] {mask(v)}")
                        shown += 1
                        break
                    if shown >= 25:
                        return 0
    finally:
        k32.CloseHandle(h)
    print(f"scanned {scanned/1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
