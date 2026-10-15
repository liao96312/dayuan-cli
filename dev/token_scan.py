#!/usr/bin/env python3
"""Read-only scan of the live engine's memory for the injected cloud token.

The engine logs "injected llm_access_token into provider api_key" and
carries a TokensConfig {access_token, refresh_token, llm_expired_at, ...}
received over the control channel. That token is what authorizes the
cloud CLI calls. If we can read it (read-only), a fully non-UI
CLI becomes possible.

Nothing is written to the target process: OpenProcess with
PROCESS_VM_READ | PROCESS_QUERY_INFORMATION and ReadProcessMemory only.

    python dev/token_scan.py            # scan live wecom-agent
    python dev/token_scan.py --pid 1234
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
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x20, 0x40}  # RO, RW, EXEC_RO, EXEC_RW


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                               ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def find_engine_pid() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process wecom-agent -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    return int(out) if out.isdigit() else 0


def regions(h: int):
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION()
    while addr < 0x7FFFFFFF:
        got = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi))
        if not got:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and not (mbi.Protect & PAGE_GUARD)):
            yield base, size
        addr = base + size
        if size == 0:
            break


def read(h: int, base: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def mask(s: str) -> str:
    return s[:10] + "..." + s[-6:] + f" (len={len(s)})" if len(s) > 20 else s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--max-region", type=int, default=64 << 20)
    args = ap.parse_args()

    pid = args.pid or find_engine_pid()
    if not pid:
        print("no wecom-agent process found")
        return 2
    print(f"target pid={pid}")

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess failed: {ctypes.get_last_error()}")
        return 3
    print("opened (read-only)")

    # JSON-ish token contexts, plus Bearer headers.
    json_pat = re.compile(
        rb'"(?:access_token|refresh_token|llm_access_token|api_key|token)"\s*:\s*"([^"]{16,400})"'
    )
    bearer_pat = re.compile(rb'Bearer\s+([A-Za-z0-9._~+/-]{20,400})')

    found: dict[str, set[str]] = {"json": set(), "bearer": set()}
    scanned = 0
    try:
        for base, size in regions(h):
            if size > args.max_region:
                continue
            data = read(h, base, size)
            if not data:
                continue
            scanned += len(data)
            for m in json_pat.finditer(data):
                found["json"].add(m.group(1).decode("ascii", "replace"))
            for m in bearer_pat.finditer(data):
                found["bearer"].add(m.group(1).decode("ascii", "replace"))
    finally:
        k32.CloseHandle(h)

    print(f"scanned {scanned/1e6:.1f} MB")
    for kind, vals in found.items():
        print(f"\n=== {kind}: {len(vals)} distinct ===")
        for v in sorted(vals)[:40]:
            print("  ", mask(v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
