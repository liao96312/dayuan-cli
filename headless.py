#!/usr/bin/env python3
"""Headless ask: drive wecom-agent with no UI.

The Oct 13 runs already completed the agent loop. Two things kept the
reply from coming back as a CLI result:

1. Startup blocks on the first POST to mcp_url. Pointing that at the
   live 9882 gateway hangs for hours (the gateway accepts then never
   answers). A local stub that answers immediately lets the engine
   reach the control channel in about a second.
2. The engine pushes the assistant text as aibot_respond_msg and waits
   for an ack whose req_id is a top-level field. Earlier peers put
   req_id only inside headers, so every ack was ignored and the send
   timed out.

This script starts the stub, a text-only LLM shim, and the WS peer,
launches wecom-agent.exe, injects one prompt, and prints the reply.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

CONFIG = r"D:\wecomdx\.weagent\agent-client-config.json"
ENGINE = r"D:\Program Files (x86)\WXWork\5.0.11.6029\WeComAgent\wecom-agent.exe"
CWD = r"D:\wecomdx"
STATE = r"D:\dayuan-cli\state"
WS_PORT = 50020
LLM_PORT = 9884
MCP_PORT = 19882

_mcp_log = os.path.join(STATE, "headless-mcp.jsonl")
_frames = os.path.join(STATE, "headless-frames.jsonl")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class McpStub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            with open(_mcp_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": raw.decode("utf-8", "replace")[:4000],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        # Any completed response unblocks startup. 502 is the fast-fail
        # the engine already tolerates (cloud tools stay unavailable).
        body = b'{"code":0,"message":"stub"}'
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


class LlmShim(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"object":"list","data":[{"id":"shim-model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            req = {}
        msgs = req.get("messages") or []
        last = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                c = m.get("content")
                last = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                break
        text = "HEADLESS-OK " + last[-180:]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        def chunk(obj: dict) -> bytes:
            return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
        self.wfile.write(chunk({"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]}))
        self.wfile.write(chunk({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *_a):
        pass


def serve(port: int, handler) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


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
    if isinstance(parsed, dict):
        msgs = parsed.get("messages") or []
    else:
        msgs = parsed
    parts = []
    for m in msgs if isinstance(msgs, list) else []:
        text = ((m.get("assistant") or {}).get("text")) or ""
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else None


def ack_for(frame: dict) -> dict:
    headers = frame.get("headers") or {}
    rid = frame.get("req_id") or headers.get("req_id") or ""
    tid = frame.get("traceid") or headers.get("traceid") or ""
    # A cmd the control channel does not whitelist is logged as
    # "unknown cmd" and never completes the outbound waiter.
    # Reply frames are the ones with req_id and no business cmd.
    return {
        "req_id": rid,
        "headers": {"req_id": rid, "traceid": tid},
        "body": {"errcode": 0, "errmsg": "", "code": 0, "message": "ok"},
    }


def patch_config() -> dict:
    original = json.load(open(CONFIG, encoding="utf-8"))
    cfg = json.loads(json.dumps(original))
    cfg.setdefault("client", {})["mcp_url"] = f"http://127.0.0.1:{MCP_PORT}/mcp"
    cfg["client"]["websocket_url"] = f"ws://127.0.0.1:{WS_PORT}"
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return original


def restore_config(original: dict | None) -> None:
    if original is None:
        return
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(original, fh, ensure_ascii=False, indent=2)


async def session(prompt: str) -> str | None:
    os.makedirs(STATE, exist_ok=True)
    for path in (_mcp_log, _frames):
        try:
            os.remove(path)
        except OSError:
            pass
    req_id = "h-" + uuid.uuid4().hex[:8]
    trace_id = "trace-" + uuid.uuid4().hex[:8]
    chatid = "conv-headless"
    inbound = {
        "cmd": "aibot_msg_callback",
        "req_id": req_id,
        "headers": {"req_id": req_id, "traceid": trace_id},
        "body": {
            "chatid": chatid,
            "msgtype": "text",
            "msgid": "m-" + uuid.uuid4().hex[:8],
            "chattype": "single",
            "text": {"content": prompt},
        },
    }
    reply: dict[str, str | None] = {"text": None}
    connected = asyncio.Event()

    async def handler(ws):
        connected.set()
        log(f"engine connected, send {prompt!r}")
        await ws.send(json.dumps(inbound, ensure_ascii=False))
        try:
            while reply["text"] is None:
                raw = await asyncio.wait_for(ws.recv(), timeout=40)
                frame = json.loads(raw)
                with open(_frames, "a", encoding="utf-8") as fh:
                    fh.write(raw if isinstance(raw, str) else raw.decode())
                    fh.write("\n")
                text = assistant_text(frame)
                if frame.get("cmd") == "aibot_respond_msg":
                    ack = ack_for(frame)
                    await ws.send(json.dumps(ack, ensure_ascii=False))
                    log(f"ack req_id={ack['req_id']!r} finish={(frame.get('body') or {}).get('stream', {}).get('finish')}")
                if text:
                    reply["text"] = text
                    if ((frame.get("body") or {}).get("stream") or {}).get("finish"):
                        return
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as exc:
            log(f"ws ended: {exc}")

    async with websockets.serve(handler, "127.0.0.1", WS_PORT):
        try:
            await asyncio.wait_for(connected.wait(), timeout=45)
        except asyncio.TimeoutError:
            log("engine never connected")
            return None
        for _ in range(180):
            if reply["text"]:
                return reply["text"]
            await asyncio.sleep(0.25)
    return reply["text"]


def launch_engine() -> subprocess.Popen:
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    env["RUST_LOG"] = "info,wecom_agent::channel::ws::outbound_ws=debug"
    log_path = os.path.join(STATE, "headless-engine.log")
    fh = open(log_path, "w", encoding="utf-8", errors="replace")
    log(f"engine log {log_path}")
    proc = subprocess.Popen(
        [ENGINE],
        cwd=CWD,
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
    )
    proc._logfh = fh  # keep the handle alive for the child
    return proc


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or "只回复这八个字：无界面测试通过"
    original = None
    proc = None
    httpds = []
    try:
        original = patch_config()
        httpds.append(serve(MCP_PORT, McpStub))
        httpds.append(serve(LLM_PORT, LlmShim))
        log(f"mcp :{MCP_PORT}  llm :{LLM_PORT}  ws :{WS_PORT}")
        proc = launch_engine()
        reply = asyncio.run(session(prompt))
        if reply:
            print(reply)
            return 0
        log("no reply")
        return 4
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for httpd in httpds:
            httpd.shutdown()
        restore_config(original)
        log("engine stopped, config restored")


if __name__ == "__main__":
    sys.exit(main())
