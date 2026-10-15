#!/usr/bin/env python3
"""Does reusing the live workspace identity make our engine authenticate?

The live engine talks to the cloud with has_authorization=false and the
gateways (9882/9883) only serve WXWork's own engine. If the identity that
authorizes those calls lives in the workspace (.session.dat, user_context,
config.json), then a scratch engine running on a COPY of that workspace
should authenticate too -- and then a fully non-UI 大圆 is ours.

Nothing here touches the live workspace: we copy, then run on the copy.

    python dev/live_workspace_test.py "只回复四个字：链路正常"
"""
from __future__ import annotations

import asyncio
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
SCRATCH = r"D:\dayuan-cli\.tmp-livews"
ROOT = os.path.join(SCRATCH, ".weagent")
WS_PORT = 50023

# Identity + config files worth carrying over; the rest regenerates.
CARRY_FILES = [
    "agent-client-config.json",
    ".session.dat",
    "config.json",
    ".user-skills.json",
    os.path.join("default", "config.json"),
    os.path.join("default", "user_context.json"),
    os.path.join("default", "user.md"),
    os.path.join("default", "memory.md"),
    os.path.join("default", "memory", "memory.md"),
    os.path.join("default", "wecom_knowledge", "collaborators.md"),
]
CARRY_DIRS = [
    os.path.join("configs", "default@9480f20df24c074e", "prompts"),
    os.path.join("configs", "default@9480f20df24c074e", "agents"),
    os.path.join("default", "presets"),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def stage() -> None:
    """Stage into SCRATCH/.weagent -- that is where the engine reads from.

    The engine resolves its workspace as <cwd>/.weagent, so the copy must
    live one level down, not at SCRATCH itself.
    """
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    global ROOT
    ROOT = os.path.join(SCRATCH, ".weagent")
    os.makedirs(ROOT, exist_ok=True)
    for rel in CARRY_FILES:
        src = os.path.join(LIVE_WS, rel)
        if os.path.isfile(src):
            dst = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            log(f"carried {rel} ({os.path.getsize(src)} bytes)")
        else:
            log(f"missing {rel}")
    for rel in CARRY_DIRS:
        src = os.path.join(LIVE_WS, rel)
        if os.path.isdir(src):
            dst = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
            log(f"carried dir {rel}")

    # Point the control channel at us, keep the real provider endpoints.
    cfgp = os.path.join(ROOT, "agent-client-config.json")
    cfg = json.load(open(cfgp, encoding="utf-8"))
    cfg.setdefault("client", {})["websocket_url"] = f"ws://127.0.0.1:{WS_PORT}"
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    log(f"patched websocket_url -> ws://127.0.0.1:{WS_PORT}")
    log(f"provider base_url = {cfg.get('server',{}).get('providers_config',{}).get('wecom',{}).get('base_url')}")


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

    req_id = "lw-" + uuid.uuid4().hex[:8]
    trace_id = "trace-" + uuid.uuid4().hex[:8]
    inbound = {
        "cmd": "aibot_msg_callback",
        "req_id": req_id,
        "headers": {"req_id": req_id, "traceid": trace_id},
        "body": {"chatid": "conv-livews", "msgtype": "text",
                 "msgid": "m-" + uuid.uuid4().hex[:8], "chattype": "single",
                 "text": {"content": prompt}},
    }
    state: dict = {"text": None, "frames": []}

    async def handler(ws):
        log("engine connected")
        await ws.send(json.dumps(inbound, ensure_ascii=False))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                frame = json.loads(raw)
                state["frames"].append(frame)
                cmd = frame.get("cmd")
                body = frame.get("body") or {}
                stream = body.get("stream") or {}
                if cmd == "overlay":
                    rid = (frame.get("headers") or {}).get("req_id", "")
                    await ws.send(json.dumps({
                        "cmd": "overlay", "headers": {"req_id": rid},
                        "body": {"type": body.get("type"),
                                 "result": {"code": 0, "message": "ok",
                                            "data": {"port": 0}}},
                    }, ensure_ascii=False))
                    continue
                log(f"<- cmd={cmd!r} finish={stream.get('finish')} keys={sorted(body)[:6]}")
                headers = frame.get("headers") or {}
                rid = frame.get("req_id") or headers.get("req_id") or ""
                if rid:
                    await ws.send(json.dumps({
                        "req_id": rid,
                        "headers": {"req_id": rid, "traceid": headers.get("traceid", "")},
                        "body": {"errcode": 0, "errmsg": "", "code": 0, "message": "ok"},
                    }, ensure_ascii=False))
                text = assistant_text(frame)
                if text:
                    state["text"] = text
                    if stream.get("finish"):
                        return
        except asyncio.TimeoutError:
            log("recv timeout")
        except Exception as exc:  # noqa: BLE001
            log(f"ws ended: {exc!r}")

    async with websockets.serve(handler, "127.0.0.1", WS_PORT, max_size=None):
        for _ in range(240):
            if state["text"]:
                break
            await asyncio.sleep(0.5)
    return state["text"], state["frames"]


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or "只回复四个字：链路正常"
    stage()

    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["RUST_LOG"] = "info,wecom_agent::infra::wecom_cli_invoker=debug,wecom_agent::runtime::openai=debug"
    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env, stdout=fh, stderr=subprocess.STDOUT)
    log(f"engine pid={proc.pid} cwd={SCRATCH}")

    text = None
    frames: list[dict] = []
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

    print("\n=== log: layout / auth / LLM ===")
    keys = ("layout_mode", "sealed file", "has_authorization", "chat_stream",
            "status error", "502", "403", "401", "Reason", "sandbox-cli",
            "approvalid", "Authorization")
    try:
        with open(logpath, encoding="utf-8", errors="replace") as f2:
            for line in f2:
                if any(k in line for k in keys):
                    sys.stdout.buffer.write(b"  " + line.rstrip("\n").encode("utf-8", "replace")[:340] + b"\n")
    except OSError as exc:
        print("  log read failed:", exc)
    return 0 if text else 4


if __name__ == "__main__":
    sys.exit(main())
