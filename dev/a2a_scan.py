#!/usr/bin/env python3
"""Scan WeCom-family live processes for A2A gateway strings.

The Agent Card advertises:
    http://<internal-a2a-host>/cgi-bin/a2a/api

System DNS can't resolve that .local name and the binaries on disk
don't contain it, so the mapping must live in runtime memory (custom
resolver / proxy config / cached CGI answer).

Read-only scan (PROCESS_VM_READ only). Dump context around every hit.

    python dev/a2a_scan.py            # all WeCom-family processes
    python dev/a2a_scan.py --pid 11340
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


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x20, 0x40}

ASCII_NEEDLES = [b"wecom-a2a", b"a2a/api", b"agent-gateway",
                 b"dayuan.wecom.work", b"a2a/api", b"/cgi-bin/a2a", b"agent-card.json"]
UTF16_NEEDLES = [n.decode().encode("utf-16-le") for n in ASCII_NEEDLES]

CTX = 192


def wproc_names() -> dict[int, str]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process | Where-Object {$_.ProcessName -match 'WXWork|wecom|WeChat'} | "
         "Select-Object Id,ProcessName | ConvertTo-Json -Compress"],
        capture_output=True, text=True).stdout.strip()
    import json
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {int(p["Id"]): p["ProcessName"] for p in data}


def dump_context(h, addr, size) -> bytes:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[:n.value]
    return b""


def scan(pid: int, name: str) -> int:
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"[{name}:{pid}] OpenProcess failed (err={ctypes.get_last_error()})")
        return 0
    hits = 0
    addr = 0
    mbi = MBI()
    while True:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if addr >= 0x7FFFFFFFFFFF:
            break
        base, size = mbi.BaseAddress or 0, mbi.RegionSize or 0
        if mbi.State == MEM_COMMIT and mbi.Protect & PAGE_GUARD == 0 and mbi.Protect in READABLE:
            data = dump_context(h, base, size)
            for needle in ASCII_NEEDLES + UTF16_NEEDLES:
                start = 0
                while True:
                    idx = data.find(needle, start)
                    if idx < 0:
                        break
                    lo = max(0, idx - CTX)
                    hi = min(len(data), idx + len(needle) + CTX)
                    snippet = data[lo:hi]
                    printable = re.sub(rb"[^\x20-\x7e]", b".", snippet).decode("ascii", "replace")
                    try:
                        utxt = snippet.decode("utf-16-le", "ignore")
                        utxt = re.sub(r"[^\x20-\x7e\u4e00-\u9fff]", ".", utxt)
                    except Exception:
                        utxt = ""
                    print(f"[{name}:{pid}] @0x{base + idx:x} needle={needle[:24]!r}")
                    print(f"    ascii: {printable}")
                    if utxt.strip(".") and uttf_ok(utxt):
                        print(f"    utf16: {utxt}")
                    hits += 1
                    start = idx + 1
                    if hits > 400:
                        print("    ... (too many hits, truncated)")
                        k32.CloseHandle(h)
                        return hits
        addr = base + size
    k32.CloseHandle(h)
    return hits


def uttf_ok(s: str) -> bool:
    return sum(c.isalnum() for c in s) >= 8


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=None)
    args = ap.parse_args()
    if args.pid:
        targets = {args.pid: "?"}
    else:
        targets = wproc_names()
    total = 0
    for pid, name in sorted(targets.items()):
        n = scan(pid, name)
        total += n
        print(f"[{name}:{pid}] done, hits={n}", file=sys.stderr)
    print(f"TOTAL hits = {total}")


if __name__ == "__main__":
    main()
