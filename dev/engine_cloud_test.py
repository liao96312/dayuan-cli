#!/usr/bin/env python3
"""Does a locally-launched engine reach the real cloud LLM (9883)?

The gray release is on, so WXWork keeps its own engine alive. If a second
engine instance can talk to 127.0.0.1:9883 (the cloud proxy), then a
non-UI 大圆 is reachable: our own control-channel peer drives the agent
loop and the real cloud model answers.

This runs the engine in a scratch workspace (never the live one) and
points only the control channel at us.  Read-only with respect to WXWork.

    python dev/engine_cloud_test.py "只回复四个字：链路正常"
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
SCRATCH = r"D:\dayuan-cli\.tmp-cloudtest"
WS_PORT = 50021
MCP_PORT = 19883
CLOUD_LLM = "http://127.0.0.1:9883/v1"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class McpStub(BaseHTTPRequestHandler):
    """The engine blocks on the first POST to mcp_url; answer fast."""

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = b'{"code":0,"message":"stub"}'
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


def build_config() -> dict:
    live = json.load(open(LIVE_CFG, encoding="utf-8"))
    server = live.get("server", {})
    providers = server.get("providers_config", {})
    # Point the provider at the real cloud proxy instead of a shim.
    if "wecom" in providers:
        providers["wecom"]["base_url"] = CLOUD_LLM
    return {
        "server": {
            "providers_config": providers,
            "defaults_config": server.get("defaults_config", {}),
            "global_override": server.get("global_override", {}),
            "llm_timeout_secs": 60,
        },
        "client": {
            "mcp_url": f"http://127.0.0.1:{MCP_PORT}/mcp",
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

    req_id = "ct-" + uuid.uuid4().hex[:8]
    trace_id = "trace-" + uuid.uuid4().hex[:8]
    inbound = {
        "cmd": "aibot_msg_callback",
        "req_id": req_id,
        "headers": {"req_id": req_id, "traceid": trace_id},
        "body": {
            "chatid": "conv-cloudtest",
            "msgtype": "text",
            "msgid": "m-" + uuid.uuid4().hex[:8],
            "chattype": "single",
            "text": {"content": prompt},
        },
    }
    state: dict = {"text": None, "frames": [], "connected": False}

    async def handler(ws):
        state["connected"] = True
        log(f"engine connected -> inject {prompt!r}")
        await ws.send(json.dumps(inbound, ensure_ascii=False))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                frame = json.loads(raw)
                state["frames"].append(frame)
                cmd = frame.get("cmd")
                body = frame.get("body") or {}
                stream = body.get("stream") or {}
                log(f"<- cmd={cmd!r} finish={stream.get('finish')} keys={sorted(body)[:8]}")
                # Ack outbound frames so the engine's sender completes.
                headers = frame.get("headers") or {}
                rid = frame.get("req_id") or headers.get("req_id") or ""
                if cmd in ("aibot_respond_msg", None) and rid:
                    await ws.send(json.dumps({
                        "req_id": rid,
                        "headers": {"req_id": rid, "traceid": headers.get("traceid", "")},
                        "body": {"errcode": 0, "errmsg": "", "code": 0, "message": "ok"},
                    }, ensure_ascii=False))
                text = assistant_text(frame)
                if text:
                    state["text"] = text
                if text and stream.get("finish"):
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

    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.join(SCRATCH, ".weagent"), exist_ok=True)
    cfg = build_config()
    with open(os.path.join(SCRATCH, ".weagent", "agent-client-config.json"), "w",
              encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    log(f"provider base_url = {cfg['server']['providers_config'].get('wecom', {}).get('base_url')}")

    httpd = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), McpStub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    env["RUST_LOG"] = "info,wecom_agent::llm=debug,wecom_agent::channel=debug"
    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env, stdout=fh, stderr=subprocess.STDOUT)
    log(f"engine pid={proc.pid} log={logpath}")

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
        httpd.shutdown()

    print("\n=== RESULT ===")
    print("reply:", text)
    print("frames:", len(frames))
    for f in frames[:6]:
        print("  ", json.dumps(f, ensure_ascii=False)[:300])

    print("\n=== engine log tail ===")
    try:
        lines = open(logpath, encoding="utf-8", errors="replace").read().splitlines()
        for line in lines[-40:]:
            print("  ", line[:260])
    except OSError as exc:
        print("  log read failed:", exc)
    return 0 if text else 4


if __name__ == "__main__":
    sys.exit(main())
