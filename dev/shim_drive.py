#!/usr/bin/env python3
"""Non-UI 大圆 driver: our engine + a scripted LLM that emits tool calls.

Findings that shape this design:

* wecom.exe / wecom-agent.exe are the SAME binary. In CLI mode it is a
  thin client for a loopback RPC server the engine only starts for its
  own Python/shell children, so we cannot call it directly.
* The engine, given the live launcher environment, DOES authenticate to
  the cloud (trust device info) and executes `wecom ...` subcommands
  in-process through its WecomCliExecutor.
* Only the LLM endpoint (9883) is reserved for WXWork's own engine, so
  our engine stalls there.

So we supply the "brain" ourselves: a local OpenAI-compatible shim that
returns tool calls. The engine runs them with full cloud authorization.

Step 1 (this file, --probe): ask the engine for a harmless read-only
contact lookup, and log the exact tool schema the engine advertises.

    python dev/shim_drive.py --probe "张三"
"""
from __future__ import annotations

import argparse
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from full_env_test import (CARRY_DIRS, CARRY_FILES, ENGINE, LIVE_WS,  # noqa: E402
                           ROOT, SCRATCH, WS_PORT, live_engine_pid, read_env)

SHIM_PORT = 19890
MCP_PORT = 19891
LOG_DIR = r"D:\dayuan-cli\state"
os.makedirs(LOG_DIR, exist_ok=True)
REQ_LOG = os.path.join(LOG_DIR, "shim-requests.jsonl")


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


# ── the scripted "brain" ────────────────────────────────────────────
class Script:
    """Turns (messages, tools) into the next assistant action.

    Each step is either a tool call or a final answer. The driver is
    deliberately explicit: we decide the commands, the engine executes
    and authorizes them.
    """

    def __init__(self, steps: list[dict]):
        self.steps = steps
        self.i = 0
        self.tool_schema_logged = False
        self.seen_results: set[str] = set()

    def next_action(self, messages: list[dict], tools: list[dict]) -> dict:
        if not self.tool_schema_logged and tools:
            with open(os.path.join(LOG_DIR, "shim-tools.json"), "w", encoding="utf-8") as fh:
                json.dump(tools, fh, ensure_ascii=False, indent=2)
            log(f"tool schemas -> state/shim-tools.json ({len(tools)} tools)")
            self.tool_schema_logged = True

        # Surface any tool result the engine fed back, verbatim.
        for m in messages:
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict))
            text = str(content or "")
            if text and text not in self.seen_results:
                self.seen_results.add(text)
                log(f"TOOL RESULT: {text[:1200]}")
                try:
                    with open(os.path.join(LOG_DIR, "shim-results.txt"), "a",
                              encoding="utf-8") as fh:
                        fh.write(text + "\n" + "-" * 60 + "\n")
                except OSError:
                    pass

        if self.i >= len(self.steps):
            return {"kind": "text", "text": "DONE"}
        step = self.steps[self.i]
        self.i += 1
        return step


SCRIPT: Script | None = None


class LlmShim(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _sse(self, obj: dict) -> bytes:
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"

    def do_GET(self):  # noqa: N802
        body = json.dumps({
            "object": "list",
            "data": [{"id": "dayuan-shim", "object": "model"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            req = {}

        messages = req.get("messages") or []
        tools = req.get("tools") or []
        try:
            with open(REQ_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(),
                    "model": req.get("model"),
                    "n_messages": len(messages),
                    "tools": len(tools),
                    "last_roles": [m.get("role") for m in messages[-6:]],
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

        action = SCRIPT.next_action(messages, tools) if SCRIPT else {"kind": "text", "text": "no script"}
        log(f"shim -> {action['kind']}: {json.dumps(action, ensure_ascii=False)[:200]}")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            if action["kind"] == "tool":
                call_id = "call_" + uuid.uuid4().hex[:12]
                args = json.dumps(action["args"], ensure_ascii=False)
                self.wfile.write(self._sse({"choices": [{"index": 0, "delta": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                    "function": {"name": action["name"], "arguments": ""}}],
                }, "finish_reason": None}]}))
                # Stream the arguments in one delta.
                self.wfile.write(self._sse({"choices": [{"index": 0, "delta": {
                    "tool_calls": [{"index": 0,
                                    "function": {"arguments": args}}],
                }, "finish_reason": None}]}))
                self.wfile.write(self._sse({"choices": [{"index": 0, "delta": {},
                                                         "finish_reason": "tool_calls"}]}))
            else:
                text = action.get("text", "")
                self.wfile.write(self._sse({"choices": [{"index": 0, "delta": {
                    "role": "assistant", "content": text}, "finish_reason": None}]}))
                self.wfile.write(self._sse({"choices": [{"index": 0, "delta": {},
                                                         "finish_reason": "stop"}]}))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError as exc:
            log(f"sse write failed: {exc!r}")

    def log_message(self, *_a):
        pass


class McpStub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        b = b'{"code":0,"message":"stub"}'
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *_a):
        pass


def stage(mcp_url: str | None = None) -> None:
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
    prov = cfg.setdefault("server", {}).setdefault("providers_config", {})
    prov.setdefault("wecom", {})
    prov["wecom"]["base_url"] = f"http://127.0.0.1:{SHIM_PORT}/v1"
    prov["wecom"]["models"] = [{
        "id": "dayuan-shim", "name": "dayuan-shim", "context_window": 128000,
        "max_tokens": 8000, "input": ["text"],
    }]
    cfg.setdefault("server", {}).setdefault("defaults_config", {})["model"] = {
        "primary": "wecom/dayuan-shim"}
    cfg.setdefault("client", {})["mcp_url"] = mcp_url or f"http://127.0.0.1:{MCP_PORT}/mcp"
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
    parts = []
    for m in msgs if isinstance(msgs, list) else []:
        a = m.get("assistant") or {}
        if a.get("text"):
            parts.append(a["text"])
    return "\n".join(parts) if parts else None


async def run_session(prompt: str, timeout_s: int) -> dict:
    import websockets

    req_id = "sh-" + uuid.uuid4().hex[:8]
    trace_id = "trace-" + uuid.uuid4().hex[:8]
    inbound = {
        "cmd": "aibot_msg_callback", "req_id": req_id,
        "headers": {"req_id": req_id, "traceid": trace_id},
        "body": {"chatid": "conv-shim", "msgtype": "text",
                 "msgid": "m-" + uuid.uuid4().hex[:8], "chattype": "single",
                 "text": {"content": prompt}},
    }
    state: dict = {"text": None, "frames": [], "done": False}

    async def handler(ws):
        log("engine connected")
        await ws.send(json.dumps(inbound, ensure_ascii=False))
        try:
            while not state["done"]:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
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
                    log(f"assistant text: {text[:200]!r}")
                if stream.get("finish"):
                    state["done"] = True
        except asyncio.TimeoutError:
            log("session timeout")
        except Exception as exc:  # noqa: BLE001
            log(f"ws ended: {exc!r}")

    async with websockets.serve(handler, "127.0.0.1", WS_PORT, max_size=None):
        for _ in range(timeout_s * 2):
            if state["done"]:
                break
            await asyncio.sleep(0.5)
    return state


def launch_engine(mcp_url: str | None = None) -> tuple[subprocess.Popen, object]:
    pid = live_engine_pid()
    live_env = read_env(pid)
    stage(mcp_url)
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
    env["RUST_LOG"] = ("info,wecom_agent::runtime::openai=debug,"
                       "wecom_agent::infra::wecom_cli_invoker=debug,"
                       "wecom_agent::runtime::wecom_cli_executor=debug")
    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env,
                            stdout=fh, stderr=subprocess.STDOUT)
    log(f"engine pid={proc.pid} log={logpath}")
    return proc, fh


def main() -> int:
    global SCRIPT
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", metavar="NAME",
                    help="read-only contact lookup for NAME")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--real-mcp", action="store_true",
                    help="use the real WXWork MCP gateway (9882) instead of a stub")
    args = ap.parse_args()
    mcp_url = "http://127.0.0.1:9882/mcp" if args.real_mcp else None

    if args.probe:
        name = args.probe
        # exec requires `command` + `execution_purpose` (50 chars max).
        payload = json.dumps({"keywords": [name]}, ensure_ascii=False)
        cmd = f"wecom contact users search --json '{payload}'"
        SCRIPT = Script([
            {"kind": "tool", "name": "exec",
             "args": {"command": cmd,
                      "execution_purpose": f"查询联系人 {name} 的信息"}},
            {"kind": "text", "text": "PROBE-DONE"},
        ])
        prompt = args.prompt or f"帮我查一下联系人 {name}"
    else:
        SCRIPT = Script([{"kind": "text", "text": "no-op"}])
        prompt = args.prompt or "hi"

    for path in (REQ_LOG,):
        try:
            os.remove(path)
        except OSError:
            pass

    shim = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), LlmShim)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    mcp = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), McpStub)
    threading.Thread(target=mcp.serve_forever, daemon=True).start()
    log(f"shim :{SHIM_PORT}  mcp stub :{MCP_PORT}  ws :{WS_PORT}")

    proc, fh = launch_engine(mcp_url)
    try:
        state = asyncio.run(run_session(prompt, args.timeout))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()
        shim.shutdown()
        mcp.shutdown()

    print("\n=== RESULT ===")
    print("assistant text:", state["text"])
    print("frames:", len(state["frames"]))

    print("\n=== engine log: exec / tool / wecom ===")
    logpath = os.path.join(SCRATCH, "engine.log")
    try:
        with open(logpath, encoding="utf-8", errors="replace") as f2:
            for line in f2:
                if any(k in line for k in ("exec", "wecom_cli", "tool_result",
                                           "ToolService", "llm", "Reason",
                                           "inject_corpwiki", "error", "ERROR")):
                    sys.stdout.buffer.write(
                        b"  " + line.rstrip("\n").encode("utf-8", "replace")[:300] + b"\n")
    except OSError as exc:
        print("  log read failed:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
