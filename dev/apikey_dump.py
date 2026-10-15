#!/usr/bin/env python3
"""Dump the full WorkBuddy message that demoed the A2A endpoint and the
public qyapi MCP bot API (with apikey) from WXWork.exe live memory.

Earlier scan (a2a_scan.py) found, in one message buffer:
    ...<internal-a2a-host>...
    https://qyapi.weixin.qq.com/mcp/v2/bot/msg?apikey=<redacted>

The 192-byte window truncated the apikey. This tool:
  1. finds every `apikey=` / `/mcp/v2/bot/msg` / `qyapi.weixin.qq.com`
     occurrence (ASCII + UTF-16),
  2. extracts the whole surrounding text run (grows outward while the
     bytes decode as printable UTF-16/ASCII text),
  3. saves everything to state/a2a_apikey_dump.txt.

Read-only (PROCESS_VM_READ). Usage:
    python dev/apikey_dump.py [--pid 11340]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys
from pathlib import Path

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

NEEDLES = [b"apikey=", b"/mcp/v2/bot/msg", b"qyapi.weixin.qq.com"]
UTF16 = [n.decode().encode("utf-16-le") for n in NEEDLES]

MAX_RUN = 65536  # max bytes of surrounding text run to extract
OUT = Path(__file__).resolve().parent.parent / "state" / "a2a_apikey_dump.txt"


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


def utf16_ok(unit: int) -> bool:
    if unit == 0:
        return False
    ch = chr(unit)
    if ch.isalnum():
        return True
    return ch in " \t\r\n:;/?.#=&_-+~%@,$!*()[]{}<>|'\"`^\\\u4e00-\u9fff\u3000-\u303f\uff00-\uffef" or \
        (0x4E00 <= unit <= 0x9FFF) or (0x3000 <= unit <= 0x303F) or (0xFF00 <= unit <= 0xFFEF)


def printable_ascii(b: int) -> bool:
    return 0x20 <= b <= 0x7E


def extract_utf16_run(data: bytes, idx: int) -> tuple[int, int]:
    """Grow [idx, idx) outward while 2-byte units are printable UTF-16."""
    start = idx
    while start >= 2 and (idx - start) < MAX_RUN:
        unit = int.from_bytes(data[start - 2:start], "little")
        if not utf16_ok(unit):
            break
        start -= 2
    end = idx
    n = len(data)
    while end + 2 <= n and (end - idx) < MAX_RUN:
        unit = int.from_bytes(data[end:end + 2], "little")
        if not utf16_ok(unit):
            break
        end += 2
    return start, end


def extract_ascii_run(data: bytes, idx: int) -> tuple[int, int]:
    start = idx
    while start > 0 and printable_ascii(data[start - 1]) and (idx - start) < MAX_RUN:
        start -= 1
    end = idx
    n = len(data)
    while end < n and printable_ascii(data[end]) and (end - idx) < MAX_RUN:
        end += 1
    return start, end


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=None)
    args = ap.parse_args()
    pid = args.pid or find_wxwork()
    if not pid:
        print("WXWork not running")
        sys.exit(1)
    print(f"scanning WXWork pid={pid}")
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        print(f"OpenProcess failed err={ctypes.get_last_error()}")
        sys.exit(1)

    chunks: list[str] = []
    seen_runs: set[tuple[int, int]] = set()
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
            for needle in NEEDLES + UTF16:
                start = 0
                while True:
                    idx = data.find(needle, start)
                    if idx < 0:
                        break
                    is_u16 = needle in UTF16
                    if is_u16:
                        run_lo, run_hi = extract_utf16_run(data, idx)
                        text = data[run_lo:run_hi].decode("utf-16-le", "replace")
                    else:
                        run_lo, run_hi = extract_ascii_run(data, idx)
                        text = data[run_lo:run_hi].decode("ascii", "replace")
                    key = (base + run_lo, base + run_hi)
                    if key not in seen_runs:
                        seen_runs.add(key)
                        chunks.append(
                            f"=== hit needle={needle!r} u16={is_u16} "
                            f"@0x{base + idx:x} run=0x{base + run_lo:x}..0x{base + run_hi:x} "
                            f"len={run_hi - run_lo} ===\n{text}\n")
                    start = idx + 1
        addr = base + size
    k32.CloseHandle(h)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(chunks), encoding="utf-8")
    print(f"runs dumped: {len(chunks)} -> {OUT}")

    # also print any complete apikey-looking tokens for quick review
    keys = set()
    for c in chunks:
        for m in re.finditer(r"apikey=([A-Za-z0-9_\-]{20,})", c):
            keys.add(m.group(1))
    for k in sorted(keys):
        print(f"apikey candidate ({len(k)} chars): {k}")


if __name__ == "__main__":
    main()
