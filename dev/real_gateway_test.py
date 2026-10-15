#!/usr/bin/env python3
"""Decisive test: can a locally-launched engine use the REAL 9882/9883?

The gray release is on, so WXWork runs its own engine and keeps the
gateways up. This launches a second engine whose control channel points
at us, but whose MCP/LLM endpoints are the real WXWork gateways.

If the real MCP answers (no 502) and the real LLM streams a reply, then
a non-UI 大圆 is reachable without touching WXWork's own engine.

    python dev/real_gateway_test.py "只回复四个字：链路正常"
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"
# WXWork per-account data dir; set DAYUAN_WXWORK_DATA_DIR to your own.
WXWORK_DATA_DIR = os.environ.get("DAYUAN_WXWORK_DATA_DIR", r"D:\文档\WXWork\<corp_id>")
LIVE_CFG = os.path.join(WXWORK_DATA_DIR, "WeComAgent", "WeComAgent", "agent-client-config.json")
SCRATCH = r"D:\dayuan-cli\.tmp-realtest"
WS_PORT = 50022
REAL_MCP = "http://127.0.0.1:9882/mcp"
REAL_LLM = "http://127.0.0.1:9883/v1"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class Spy(BaseHTTPRequestHandler):
    """Stands in for the browser MCP port probe; records anything it sees."""

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        log(f"SPY {self.path} <- {raw[:200]!r}")
        body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


def build_config() -> dict:
    live = json.load(open(LIVE_CFG, encoding="utf-8"))
    server = live.get("server", {})
    providers = server.get("providers_config", {})
    if "wecom" in providers:
        providers["wecom"]["base_url"] = REAL_LLM
    return {
        "server": {
            "providers_config": providers,
            "defaults_config": server.get("defaults_config", {}),
            "global_override": server.get("global_override", {}),
            "llm_timeout_secs": 60,
        },
        "client": {
            "mcp_url": REAL_MCP,
            "websocket_url": f"ws://127.0.0.1:{WS_PORT}",
        },
    }


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
    parts = []
    for m in msgs if isinstance(msgs, list) else []:
        t = ((m.get("assistant") or {}).get("text")) or ""
        if t:
            parts.append(t)
    return "\n".join(parts) if parts else None


async def session(prompt: str) -> tuple[str | None, list[dict]]:
    import websockets

    req_id = "rt-" + uuid.uuid4().hex[:8]
    trace_id = "trace-" + uuid.uuid4().hex[:8]
    inbound = {
        "cmd": "aibot_msg_callback",
        "req_id": req_id,
        "headers": {"req_id": req_id, "traceid": trace_id},
        "body": {
            "chatid": "conv-realtest",
            "msgtype": "text",
            "msgid": "m-" + uuid.uuid4().hex[:8],
            "chattype": "single",
            "text": {"content": prompt},
        },
    }
    state: dict = {"text": None, "frames": []}

    async def handler(ws):
        log("engine connected")
        await ws.send(json.dumps(inbound, ensure_ascii=False))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
                frame = json.loads(raw)
                state["frames"].append(frame)
                cmd = frame.get("cmd")
                body = frame.get("body") or {}
                stream = body.get("stream") or {}
                if cmd == "overlay":
                    # Answer the browser-port probe so startup is not delayed.
                    rid = (frame.get("headers") or {}).get("req_id", "")
                    await ws.send(json.dumps({
                        "cmd": "overlay",
                        "headers": {"req_id": rid},
                        "body": {"type": body.get("type"),
                                 "result": {"code": 0, "message": "ok", "data": {"port": 0}}},
                    }, ensure_ascii=False))
                    continue
                log(f"<- cmd={cmd!r} finish={stream.get('finish')}")
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
        for _ in range(200):
            if state["text"]:
                break
            await asyncio.sleep(0.5)
    return state["text"], state["frames"]


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or "只回复四个字：链路正常"

    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.join(SCRATCH, ".weagent"), exist_ok=True)
    cfg = build_config()
    with open(os.path.join(SCRATCH, ".weagent", "agent-client-config.json"), "w",
              encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    log(f"mcp={REAL_MCP} llm={REAL_LLM} ws=ours")

    spy = ThreadingHTTPServer(("127.0.0.1", 53796), Spy)
    threading.Thread(target=spy.serve_forever, daemon=True).start()

    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["RUST_LOG"] = "info,wecom_agent::infra::wecom_cli_invoker=debug,wecom_agent::runtime::openai=debug"
    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env, stdout=fh, stderr=subprocess.STDOUT)
    log(f"engine pid={proc.pid}")

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
        spy.shutdown()

    print("\n=== RESULT ===")
    print("reply:", text)
    print("frames:", len(frames))

    print("\n=== key log lines ===")
    keys = ("502", "chat_stream", "status error", "wecom_cli_invoker", "sandbox-cli",
            "LLM", "Reason", "reply", "assistant")
    try:
        with open(logpath, encoding="utf-8", errors="replace") as f2:
            for line in f2:
                if any(k in line for k in keys):
                    sys.stdout.buffer.write(b"  " + line.rstrip("\n").encode("utf-8", "replace")[:300] + b"\n")
    except OSError as exc:
        print("  log read failed:", exc)
    return 0 if text else 4


if __name__ == "__main__":
    sys.exit(main())
