#!/usr/bin/env python3
"""UI-free control-channel peer for wecom-agent (大圆 engine).

This is the non-UI replacement for dayuan-cli's click/OCR path.
It plays the role of WeCom's control-channel WS server:

  inbound  : {"cmd":"aibot_msg_callback", ...}   -> engine agent loop
  outbound : {"cmd":"aibot_respond_msg", ...}    -> assistant text (no OCR needed)
  ack      : {"cmd":"notify_approval", ...}       -> best effort, see notes

Usage:
  python proto.py --port 50020            # interactive REPL
  python proto.py --port 50020 --once "你好"  # single shot, exits after reply

Prerequisite: the engine's agent-client-config.json must have
  client.websocket_url = ws://127.0.0.1:<port>
and a working LLM provider (see llm_shim.py / a real OpenAI-compatible endpoint).
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import websockets

CONFIG = r"D:\wecomdx\.weagent\agent-client-config.json"
LOG = r"D:\dayuan-cli\state\proto.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def frame(cmd: str, req_id: str, trace_id: str, body) -> str:
    return json.dumps({"cmd": cmd, "headers": {"req_id": req_id, "traceid": trace_id},
                       "body": body}, ensure_ascii=False)


def assistant_text(j: dict) -> str | None:
    """Extract assistant text from an aibot_respond_msg frame."""
    body = j.get("body") or {}
    stream = body.get("stream") or {}
    tj = stream.get("think_json")
    if not tj:
        return None
    try:
        msgs = json.loads(tj) if isinstance(tj, str) else tj
    except (ValueError, TypeError):
        return None
    out = []
    for m in msgs if isinstance(msgs, list) else []:
        a = m.get("assistant") or {}
        t = a.get("text")
        if t:
            out.append(t)
    return "\n".join(out) if out else None


def patch_config(port: int) -> None:
    try:
        d = json.load(open(CONFIG, encoding="utf-8"))
    except OSError as e:
        log(f"config read failed: {e}")
        return
    d.setdefault("client", {})["websocket_url"] = f"ws://127.0.0.1:{port}"
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    log(f"config patched: websocket_url=ws://127.0.0.1:{port}")


async def session(ws, prompt: str, chatid: str) -> str | None:
    req_id = uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex[:12]
    body = {"chatid": chatid, "msgtype": "text", "msgid": uuid.uuid4().hex[:12],
            "chattype": "single", "text": {"content": prompt}}
    await ws.send(frame("aibot_msg_callback", req_id, trace_id, body))
    log(f"-> {prompt!r} (trace={trace_id})")
    reply = None
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            j = json.loads(raw)
            if (j.get("headers") or {}).get("req_id") != req_id:
                continue
            text = assistant_text(j)
            # best-effort ack (shape still not fully matched by the engine)
            await ws.send(json.dumps({"cmd": "notify_approval", "trace_id": trace_id,
                                      "action_id": req_id, "retcode": 0}, ensure_ascii=False))
            if text:
                reply = text
                # engine marks the run complete on the finish frame
                if ((j.get("body") or {}).get("stream") or {}).get("finish"):
                    break
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as e:
        log(f"wait ended: {e}")
    return reply


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=50020)
    ap.add_argument("--chatid", default="conv-cli")
    ap.add_argument("--once", help="single prompt, then exit")
    ap.add_argument("--no-patch", action="store_true", help="do not rewrite engine config")
    args = ap.parse_args()

    if not args.no_patch:
        patch_config(args.port)

    state = {"prompt": args.once, "done": False}

    async def handler(ws):
        if state["done"]:
            return
        state["done"] = True
        reply = await session(ws, state["prompt"] or "（交互模式需要 stdin 驱动，见 README）", args.chatid)
        print(f"REPLY: {reply}")

    async with websockets.serve(handler, "127.0.0.1", args.port):
        log(f"control-channel peer listening on {args.port}")
        if args.once:
            await asyncio.sleep(180)
        else:
            await asyncio.sleep(10**6)


if __name__ == "__main__":
    asyncio.run(main())
