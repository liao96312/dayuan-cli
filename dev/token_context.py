#!/usr/bin/env python3
"""Context-aware, read-only memory scan for the injected cloud token.

The engine receives TokensConfig {access_token, refresh_token,
llm_expired_at, ...} and logs "injected llm_access_token into provider
api_key". The value authorizes the local 9883 proxy. Earlier scans looked
for quoted JSON only; this dumps the bytes AROUND every occurrence of the
key names, so whatever shape the value has, it shows up.

Read-only: OpenProcess(PROCESS_VM_READ|QUERY) + ReadProcessMemory.

    python dev/token_context.py --pid 14028
    python dev/token_context.py --pid 11340
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
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x20, 0x40}

KEYS = [b"access_token", b"llm_access_token", b"refresh_token", b"api_key",
        b"Authorization", b"Bearer ", b"llm_expired_at"]


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def regions(h):
    addr, mbi = 0, MBI()
    while addr < 0x7FFFFFFF:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base, size = mbi.BaseAddress or 0, mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and not (mbi.Protect & PAGE_GUARD) and size <= (256 << 20)):
            yield base, size
        addr = base + size
        if size == 0:
            break


def read(h, base, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def find_pid(name: str) -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process {name} -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    return int(out) if out.isdigit() else 0


def mask(s: str) -> str:
    return s if len(s) <= 24 else f"{s[:12]}...{s[-8:]} (len={len(s)})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--name", default="wecom-agent")
    ap.add_argument("--context", type=int, default=160)
    ap.add_argument("--max-hits", type=int, default=40)
    args = ap.parse_args()

    pid = args.pid or find_pid(args.name)
    if not pid:
        print(f"no process {args.name}")
        return 2
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        return 3
    print(f"pid={pid} ({args.name}) opened read-only")

    hits: list[str] = []
    scanned = 0
    try:
        for base, size in regions(h):
            data = read(h, base, size)
            if not data:
                continue
            scanned += len(data)
            for key in KEYS:
                for m in re.finditer(re.escape(key), data):
                    s = max(0, m.start() - args.context)
                    e = min(len(data), m.end() + args.context)
                    chunk = data[s:e]
                    txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
                    if txt not in hits:
                        hits.append(txt)
                    if len(hits) >= args.max_hits:
                        break
                if len(hits) >= args.max_hits:
                    break
            if len(hits) >= args.max_hits:
                break
    finally:
        k32.CloseHandle(h)

    print(f"scanned {scanned/1e6:.1f} MB, {len(hits)} contexts\n")
    for i, txt in enumerate(hits, 1):
        print(f"--- hit {i} ---")
        print(txt)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
