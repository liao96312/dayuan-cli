#!/usr/bin/env python3
"""Locate the resolved provider config (api_key injected) in memory.

The engine logs "injected llm_access_token into provider api_key". The
resolved config therefore holds the model id and the key together, in
memory, adjacent. Earlier scans looked for key NAMES; this dumps the
bytes around the MODEL NAME instead.

Read-only: OpenProcess(PROCESS_VM_READ|QUERY) + ReadProcessMemory.

    python dev/provider_scan.py --pid 14028
    python dev/provider_scan.py --pid 11340
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

NEEDLES = [b"MiniMax-M2.5-WW-128K-Test", b"HY-vision-1.5-instruct",
           b"openai-completions", b"wecom/MiniMax"]

TOKENISH = re.compile(rb"[A-Za-z0-9_\-\.=+/]{28,220}")


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
                and not (mbi.Protect & PAGE_GUARD) and size <= (512 << 20)):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--name", default="wecom-agent")
    ap.add_argument("--window", type=int, default=900)
    ap.add_argument("--max", type=int, default=6)
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

    shown = 0
    scanned = 0
    try:
        for base, size in regions(h):
            data = read(h, base, size)
            if not data:
                continue
            scanned += len(data)
            for needle in NEEDLES:
                idx = data.find(needle)
                if idx < 0:
                    continue
                s = max(0, idx - args.window // 2)
                e = min(len(data), idx + args.window)
                chunk = data[s:e]
                txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
                print(f"=== {needle.decode()} @ region 0x{base:x}+{idx} ===")
                print(txt)
                # Token-shaped strings inside the window.
                toks = [m.group().decode() for m in TOKENISH.finditer(chunk)]
                toks = [t for t in toks if not t.startswith(("MiniMax", "HY-", "openai"))
                        and len(t) >= 32]
                if toks:
                    print("  [token-shaped strings in window]")
                    for t in toks[:12]:
                        print(f"    {t[:24]}...{t[-8:]} (len={len(t)})")
                print()
                shown += 1
                if shown >= args.max:
                    return 0
    finally:
        k32.CloseHandle(h)
    print(f"scanned {scanned/1e6:.1f} MB, {shown} windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
