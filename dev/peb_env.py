#!/usr/bin/env python3
"""Read a live process's environment block (read-only).

The live wecom-agent is launched by WXWork with no argv, so its contract
(workspace root, control-channel token, RPC settings) must arrive via the
environment. Reading the PEB gives us that contract.

Read-only: OpenProcess(PROCESS_VM_READ|QUERY), NtQueryInformationProcess,
ReadProcessMemory. Nothing is written.

    python dev/peb_env.py --pid 14028
    python dev/peb_env.py --name WXWork
"""
from __future__ import annotations

import argparse
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


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                ("Buffer", ctypes.c_void_p)]


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


def find_pid(name: str) -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process {name} -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    return int(out) if out.isdigit() else 0


def rd(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--name", default="wecom-agent")
    ap.add_argument("--filter", default="")
    args = ap.parse_args()

    pid = args.pid or find_pid(args.name)
    if not pid:
        print(f"no process {args.name}")
        return 2
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        return 3
    print(f"pid={pid} ({args.name}) opened read-only\n")

    pbi = PROCESS_BASIC_INFORMATION()
    status = ntdll.NtQueryInformationProcess(
        wt.HANDLE(h), 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None)
    if status != 0:
        print(f"NtQueryInformationProcess failed: 0x{status & 0xFFFFFFFF:x}")
        return 4
    peb = pbi.PebBaseAddress
    print(f"PEB @ 0x{peb:x}")

    # PEB->ProcessParameters at +0x20 (x64); RTL_USER_PROCESS_PARAMETERS
    # ->Environment at +0x80.
    pp_raw = rd(h, peb + 0x20, 8)
    if not pp_raw:
        print("cannot read ProcessParameters")
        return 5
    pp = int.from_bytes(pp_raw, "little")
    print(f"ProcessParameters @ 0x{pp:x}")

    env_raw = rd(h, pp + 0x80, 8)
    if not env_raw:
        print("cannot read Environment pointer")
        return 6
    env_ptr = int.from_bytes(env_raw, "little")
    print(f"Environment @ 0x{env_ptr:x}\n")

    data = None
    for size in (256 * 1024, 64 * 1024, 16 * 1024, 4096):
        data = rd(h, env_ptr, size)
        if data:
            break
    if not data:
        print("cannot read environment block")
        return 7

    # Environment is UTF-16LE, NUL-separated, double-NUL terminated.
    text = data.decode("utf-16-le", "replace")
    entries = [e for e in text.split("\x00") if e.strip()]
    k32.CloseHandle(h)

    print(f"=== {len(entries)} env entries ===")
    needle = args.filter.lower()
    for e in entries:
        if needle and needle not in e.lower():
            continue
        print("  ", e[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
