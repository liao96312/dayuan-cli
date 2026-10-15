#!/usr/bin/env python3
"""Find the real IP WXWork uses for the 大圆 cloud host.

Our DNS resolves the cloud host to an internal address, which is
unreachable from this host, yet WXWork clearly talks to the cloud. The
real address must be somewhere in the live processes (a hardcoded IP, a
custom resolver, or a cached DNS record).

Read-only memory scan: dump context around every cloud-host occurrence.

    python dev/host_scan.py
"""
from __future__ import annotations

import argparse
import os
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class PBI(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x20, 0x40}

CLOUD_HOST = os.environ.get("DAYUAN_CLOUD_HOST", "<cloud-host>")
NEEDLES = [CLOUD_HOST.encode(), b"/cgi-bin/claw"]
IPLIKE = re.compile(rb"(?<![0-9])(?:(?:2[0-5]{2}|1\d\d|[1-9]?\d)\.){3}(?:2[0-5]{2}|1\d\d|[1-9]?\d)(?![0-9])")


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


def scan(pid: int, max_hits: int) -> list[str]:
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"  OpenProcess({pid}) failed")
        return []
    out: list[str] = []
    total = 0
    try:
        for base, size in regions(h):
            data = rd(h, base, size)
            if not data:
                continue
            total += len(data)
            for needle in NEEDLES:
                for m in re.finditer(re.escape(needle), data):
                    s = max(0, m.start() - 260)
                    e = min(len(data), m.end() + 300)
                    chunk = data[s:e]
                    txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
                    ips = [m2.group().decode() for m2 in IPLIKE.finditer(chunk)]
                    rec = txt + (f"\n     IPs nearby: {sorted(set(ips))}" if ips else "")
                    if rec not in out:
                        out.append(rec)
                if len(out) >= max_hits:
                    return out, total
    except Exception as exc:  # noqa: BLE001
        out.append(f"scan error: {exc!r}")
    finally:
        k32.CloseHandle(h)
    return out, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--name", default="WXWork")
    ap.add_argument("--max", type=int, default=8)
    args = ap.parse_args()

    pid = args.pid or (subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process {args.name} -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20).stdout.strip())
    pid = int(pid) if str(pid).isdigit() else 0
    if not pid:
        print(f"no process {args.name}")
        return 2
    print(f"scanning pid={pid} ({args.name}) for cloud host refs\n")
    hits, total = scan(pid, args.max)
    print(f"scanned {total/1e6:.1f} MB, {len(hits)} context(s)\n")
    for i, txt in enumerate(hits, 1):
        print(f"--- {i} ---")
        print(txt)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
