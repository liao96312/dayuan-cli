#!/usr/bin/env python3
"""Launch our own engine with the LIVE launcher contract.

Read from the live wecom-agent's environment block (read-only):
  WECOM_AGENT_TRUST_DEVICE_INFO  -- device trust blob (cloud auth)
  WECOM_AGENT_DEVICE_ID
  WECOM_USER_INFO                -- {"name":..., "vid":...}
  WECOMAGENT_SANDBOX_CLI_PATH, WECOMAGENT_PYTHON_HOME, ...

Our earlier standalone runs lacked all of these, which is why they showed
has_authorization=false and no cloud reach. This run supplies them, keeps
the control channel pointed at us, and reports whether the engine can now
reach the cloud LLM.

    python dev/full_env_test.py "只回复四个字：链路正常"
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

ENGINE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"
# WXWork per-account data dir; set DAYUAN_WXWORK_DATA_DIR to your own.
WXWORK_DATA_DIR = os.environ.get("DAYUAN_WXWORK_DATA_DIR", r"D:\文档\WXWork\<corp_id>")
LIVE_WS = os.path.join(WXWORK_DATA_DIR, "WeComAgent", "WeComAgent")
SCRATCH = r"D:\dayuan-cli\.tmp-fullenv"
ROOT = os.path.join(SCRATCH, ".weagent")
WS_PORT = 50024
MCP_PORT = 19885

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class PBI(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2), ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


def rd(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return buf.raw[: n.value]
    return None


def read_env(pid: int) -> dict[str, str]:
    h = k32.OpenProcess(0x0410, False, pid)  # QUERY | VM_READ
    if not h:
        raise SystemExit(f"OpenProcess({pid}) failed")
    pbi = PBI()
    ntdll.NtQueryInformationProcess(wt.HANDLE(h), 0, ctypes.byref(pbi),
                                    ctypes.sizeof(pbi), None)
    pp = int.from_bytes(rd(h, pbi.PebBaseAddress + 0x20, 8), "little")
    env_ptr = int.from_bytes(rd(h, pp + 0x80, 8), "little")
    data = None
    for size in (256 * 1024, 64 * 1024, 16 * 1024, 4096):
        data = rd(h, env_ptr, size)
        if data:
            break
    k32.CloseHandle(h)
    text = data.decode("utf-16-le", "replace")
    out: dict[str, str] = {}
    for e in text.split("\x00"):
        if "=" in e:
            k, v = e.split("=", 1)
            out[k] = v
    return out


def live_engine_pid() -> int:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process wecom-agent -ErrorAction SilentlyContinue | "
         "Sort-Object StartTime | Select-Object -First 1).Id"],
        capture_output=True, text=True, timeout=20).stdout.strip()
    return int(out) if out.isdigit() else 0


CARRY_FILES = ["agent-client-config.json", ".session.dat",
               os.path.join("default", "config.json"),
               os.path.join("default", "user_context.json"),
               os.path.join("default", "user.md"),
               os.path.join("default", "memory.md")]
CARRY_DIRS = [os.path.join("configs", "default@9480f20df24c074e", "prompts"),
              os.path.join("configs", "default@9480f20df24c074e", "agents"),
              os.path.join("default", "presets"),
              os.path.join("default", "skills")]


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


def assistant_text(frame: dict) -> str | None:
    body = frame.get("body") or {}
    stream = body.get("stream") or {}
    tj = stream.get("think_json")
    if not tj:
        return None
    try:
        parsed = json.loads(tj) if isinstance(tj, str) else tj
    except (ValueError, TypeError):
        return None
    msgs = parsed.get("messages") if isinstance(parsed, dict) else parsed
    parts = [((m.get("assistant") or {}).get("text") or "") for m in (msgs or [])]
    parts = [p for p in parts if p]
    return "\n".join(parts) if parts else None


async def session(prompt: str) -> tuple[str | None, list[dict]]:
    import websockets

    req_id = "fe-" + uuid.uuid4().hex[:8]
    trace_id = "trace-" + uuid.uuid4().hex[:8]
    inbound = {
        "cmd": "aibot_msg_callback", "req_id": req_id,
        "headers": {"req_id": req_id, "traceid": trace_id},
        "body": {"chatid": "conv-fullenv", "msgtype": "text",
                 "msgid": "m-" + uuid.uuid4().hex[:8], "chattype": "single",
                 "text": {"content": prompt}},
    }
    state: dict = {"text": None, "frames": []}

    async def handler(ws):
        print("[ws] engine connected", file=sys.stderr, flush=True)
        await ws.send(json.dumps(inbound, ensure_ascii=False))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                frame = json.loads(raw)
                state["frames"].append(frame)
                cmd, body = frame.get("cmd"), frame.get("body") or {}
                stream = body.get("stream") or {}
                if cmd == "overlay":
                    rid = (frame.get("headers") or {}).get("req_id", "")
                    await ws.send(json.dumps({
                        "cmd": "overlay", "headers": {"req_id": rid},
                        "body": {"type": body.get("type"),
                                 "result": {"code": 0, "message": "ok",
                                            "data": {"port": 0}}}}, ensure_ascii=False))
                    continue
                print(f"[ws] <- {cmd} finish={stream.get('finish')}",
                      file=sys.stderr, flush=True)
                headers = frame.get("headers") or {}
                rid = frame.get("req_id") or headers.get("req_id") or ""
                if rid:
                    await ws.send(json.dumps({
                        "req_id": rid,
                        "headers": {"req_id": rid, "traceid": headers.get("traceid", "")},
                        "body": {"errcode": 0, "errmsg": "", "code": 0, "message": "ok"}},
                        ensure_ascii=False))
                text = assistant_text(frame)
                if text:
                    state["text"] = text
                    if stream.get("finish"):
                        return
        except asyncio.TimeoutError:
            print("[ws] recv timeout", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[ws] ended: {exc!r}", file=sys.stderr, flush=True)

    async with websockets.serve(handler, "127.0.0.1", WS_PORT, max_size=None):
        for _ in range(240):
            if state["text"]:
                break
            await asyncio.sleep(0.5)
    return state["text"], state["frames"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="*", default=["只回复四个字：链路正常"])
    args = ap.parse_args()
    prompt = " ".join(args.prompt).strip() or "只回复四个字：链路正常"

    pid = live_engine_pid()
    if not pid:
        print("live engine not running")
        return 2
    live_env = read_env(pid)
    print(f"read {len(live_env)} env vars from live engine pid={pid}")

    keys = ["WECOM_AGENT_TRUST_DEVICE_INFO", "WECOM_AGENT_DEVICE_ID",
            "WECOM_USER_INFO", "WECOMAGENT_SANDBOX_CLI_PATH",
            "WECOMAGENT_PYTHON_HOME", "WECOMAGENT_MULTICALL_BIN_DIR",
            "WECOM_AGENT_WEWORK_VERSION"]
    for k in keys:
        v = live_env.get(k, "")
        print(f"  {k} = {v[:70]}{'...' if len(v) > 70 else ''}")

    stage()

    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    for k in keys:
        if live_env.get(k):
            env[k] = live_env[k]
    # Our own workspace + logs.
    env["WECOM_AGENT_WORKSPACE_DIR"] = ROOT
    env["WECOM_AGENT_LOG_DIR"] = os.path.join(SCRATCH, "log")
    os.makedirs(env["WECOM_AGENT_LOG_DIR"], exist_ok=True)
    env["RUST_LOG"] = ("info,wecom_agent::infra::wecom_cli_invoker=debug,"
                       "wecom_agent::runtime::openai=debug,wecom_agent::app::loader_handle=debug")

    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env,
                            stdout=fh, stderr=subprocess.STDOUT)
    print(f"\nengine pid={proc.pid} cwd={SCRATCH}\n", flush=True)

    text, frames = None, []
    try:
        text, frames = asyncio.run(session(prompt))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()

    print("\n=== RESULT ===")
    print("reply:", text)
    print("frames:", len(frames))

    print("\n=== log highlights ===")
    interesting = ("layout_mode", "workspace_root", "has_authorization", "chat_stream",
                   "status error", " 401", " 403", " 502", "Reason", "trust",
                   "device_id", "inject", "api_key", "LlmAuth", "Authorization")
    try:
        with open(logpath, encoding="utf-8", errors="replace") as f2:
            for line in f2:
                if any(k in line for k in interesting):
                    sys.stdout.buffer.write(
                        b"  " + line.rstrip("\n").encode("utf-8", "replace")[:320] + b"\n")
    except OSError as exc:
        print("  log read failed:", exc)
    return 0 if text else 4


if __name__ == "__main__":
    sys.exit(main())
