#!/usr/bin/env python3
"""Find a contact's userid (wo-prefixed) near their display name in WXWork memory.

    python dev/name_scan.py --name 张三
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import subprocess

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

USERID = re.compile(r"(wo[A-Za-z0-9]{28,34})")


def find_wxwork() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process WXWork -ErrorAction SilentlyContinue | Select-Object -First 1).Id"],
        capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def read(h, addr, size) -> bytes:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[:n.value]
    return b""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--pid", type=int, default=None)
    args = ap.parse_args()

    pid = args.pid or find_wxwork()
    needle = args.name.encode("utf-16-le")
    needle8 = args.name.encode("utf-8")
    print(f"scanning WXWork pid={pid} for {args.name!r} (utf16+utf8)")

    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess failed err={ctypes.get_last_error()}")
        raise SystemExit(1)

    found: dict[str, set[str]] = {}
    ctx_dump: list[str] = []
    addr = 0
    mbi = MBI()
    while True:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if addr >= 0x7FFFFFFFFFFF:
            break
        base, size = mbi.BaseAddress or 0, mbi.RegionSize or 0
        if mbi.State == MEM_COMMIT and mbi.Protect & PAGE_GUARD == 0 and mbi.Protect in READABLE:
            data = read(h, base, size)
            for nd in (needle, needle8):
                start = 0
                while True:
                    idx = data.find(nd, start)
                    if idx < 0:
                        break
                    lo = max(0, idx - 400)
                    hi = min(len(data), idx + len(nd) + 400)
                    ctx = data[lo:hi]
                    is_u16 = nd == needle
                    if is_u16:
                        text = ctx.decode("utf-16-le", "replace")
                    else:
                        text = ctx.decode("utf-8", "replace")
                    ids = set(USERID.findall(text))
                    for i in ids:
                        found.setdefault(i, set()).add(
                            f"u16" if is_u16 else "u8")
                    if ids or len(ctx_dump) < 12:
                        ctx_dump.append(
                            f"@0x{base + idx:x} u16={is_u16} ids={sorted(ids)}\n"
                            f"  {text[:360]!r}")
                    start = idx + 1
        addr = base + size
    k32.CloseHandle(h)

    print(f"\nuserids near {args.name!r}:")
    for i, kinds in sorted(found.items(), key=lambda kv: -len(kv[1])):
        print(f"  {i}  ({', '.join(sorted(kinds))})")
    print("\ncontext samples:")
    for c in ctx_dump[:12]:
        print(c)


if __name__ == "__main__":
    main()
