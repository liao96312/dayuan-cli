#!/usr/bin/env python3
"""Launch our engine with the live env, then hunt for its internal RPC.

With the live launcher contract the engine authenticates to the cloud.
Its Python host exposes a loopback NDJSON RPC server (WECOMAGENT_RPC_*)
whose `wecom.exec` method runs the real `wecom` CLI -- i.e. the message
send path, with no LLM involved.

This finds that endpoint: it watches the engine's listening ports and
scans its memory for the RPC host/port/token it injects into children.

    python dev/rpc_hunt.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from full_env_test import (CARRY_DIRS, CARRY_FILES, LIVE_WS, MCP_PORT,  # noqa: E402
                           ENGINE, ROOT, SCRATCH, WS_PORT, live_engine_pid,
                           read_env)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
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


def rd(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def scan_rpc(pid: int) -> list[str]:
    """Find RPC host/port/token strings in the engine's memory."""
    h = k32.OpenProcess(0x0410, False, pid)
    if not h:
        return []
    out: list[str] = []
    try:
        for base, size in regions(h):
            data = rd(h, base, size)
            if not data:
                continue
            for m in re.finditer(rb"WECOMAGENT_RPC_(?:HOST|PORT|TOKEN)", data):
                s = max(0, m.start() - 100)
                e = min(len(data), m.end() + 300)
                txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
                if txt not in out:
                    out.append(txt)
    finally:
        k32.CloseHandle(h)
    return out


def listen_ports(pid: int) -> set[int]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -State Listen -OwningProcess {pid} "
             "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort"],
            capture_output=True, text=True, timeout=20).stdout.split()
        return {int(p) for p in out if p.isdigit()}
    except Exception:
        return set()


def stage() -> None:
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    for rel in CARRY_FILES:
        src = os.path.join(LIVE_WS, rel)
        if os.path.isfile(src):
            dst = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    for rel in CARRY_DIRS:
        src = os.path.join(LIVE_WS, rel)
        if os.path.isdir(src):
            dst = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
    cfgp = os.path.join(ROOT, "agent-client-config.json")
    cfg = json.load(open(cfgp, encoding="utf-8"))
    cfg.setdefault("client", {})["mcp_url"] = f"http://127.0.0.1:{MCP_PORT}/mcp"
    cfg["client"]["websocket_url"] = f"ws://127.0.0.1:{WS_PORT}"
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def main() -> int:
    pid = live_engine_pid()
    live_env = read_env(pid)
    stage()

    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    for k in ("WECOM_AGENT_TRUST_DEVICE_INFO", "WECOM_AGENT_DEVICE_ID",
              "WECOM_USER_INFO", "WECOMAGENT_SANDBOX_CLI_PATH",
              "WECOMAGENT_PYTHON_HOME", "WECOMAGENT_MULTICALL_BIN_DIR",
              "WECOM_AGENT_WEWORK_VERSION"):
        if live_env.get(k):
            env[k] = live_env[k]
    env["WECOM_AGENT_WORKSPACE_DIR"] = ROOT
    env["WECOM_AGENT_LOG_DIR"] = os.path.join(SCRATCH, "log")
    os.makedirs(env["WECOM_AGENT_LOG_DIR"], exist_ok=True)
    env["RUST_LOG"] = "info,wecom_agent::runtime::python_host=debug,wecom_agent::runtime::client_shell=debug"

    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env,
                            stdout=fh, stderr=subprocess.STDOUT)
    print(f"engine pid={proc.pid}\n")

    try:
        for i in range(25):
            time.sleep(2)
            if proc.poll() is not None:
                print("engine exited")
                break
            ports = listen_ports(proc.pid)
            rpc = scan_rpc(proc.pid)
            print(f"[{i*2:>3}s] listen={sorted(ports)} rpc_strings={len(rpc)}")
            for r in rpc[:3]:
                print("       ", r[:260])
            if rpc:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()

    print("\n=== log: python_host / rpc / client_shell ===")
    try:
        with open(logpath, encoding="utf-8", errors="replace") as f2:
            for line in f2:
                if any(k in line for k in ("python_host", "python_rpc", "rpc_port",
                                           "client_shell", "Client Shell", "listener")):
                    sys.stdout.buffer.write(b"  " + line.rstrip("\n").encode("utf-8", "replace")[:300] + b"\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
