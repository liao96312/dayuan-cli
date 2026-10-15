#!/usr/bin/env python3
"""Can we obtain a direct RPC server for the wecom CLI?

The bundled wecom.exe is a thin client for a loopback NDJSON RPC server
(hello/welcome + {id,method,params} -> {id,result}). The engine starts
that server for Client Shell work, gated on WECOMAGENT_SANDBOX_CLI_PATH
(we found sandbox-cli.exe next to the engine).

This launches the engine with the sandbox CLI enabled and a FIXED RPC
port/token of our choosing, then probes that port. If the engine honours
them, we have a direct non-UI channel to the real 大圆 CLI.

    python dev/rpc_direct_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"
SANDBOX_CLI = r"D:\Program Files (x86)\WXWork\5.0.12.6018\sandbox-cli.exe"
WECOM_BIN = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom.exe"
# WXWork per-account data dir; set DAYUAN_WXWORK_DATA_DIR to your own.
WXWORK_DATA_DIR = os.environ.get("DAYUAN_WXWORK_DATA_DIR", r"D:\文档\WXWork\<corp_id>")
LIVE_WS = os.path.join(WXWORK_DATA_DIR, "WeComAgent", "WeComAgent")
SCRATCH = r"D:\dayuan-cli\.tmp-rpcdirect"
ROOT = os.path.join(SCRATCH, ".weagent")

RPC_HOST = "127.0.0.1"
RPC_PORT = 50031
RPC_TOKEN = "dayuan-cli-token-0123456789abcdef"
WS_PORT = 50032
MCP_PORT = 19884

CARRY = [
    "agent-client-config.json", ".session.dat",
    os.path.join("default", "config.json"),
    os.path.join("default", "user_context.json"),
    os.path.join("default", "user.md"),
    os.path.join("default", "memory.md"),
]


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


class Stub(BaseHTTPRequestHandler):
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


def stage() -> None:
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    for rel in CARRY:
        src = os.path.join(LIVE_WS, rel)
        if os.path.isfile(src):
            dst = os.path.join(ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    cfgp = os.path.join(ROOT, "agent-client-config.json")
    cfg = json.load(open(cfgp, encoding="utf-8"))
    cfg.setdefault("client", {})["mcp_url"] = f"http://127.0.0.1:{MCP_PORT}/mcp"
    cfg["client"]["websocket_url"] = f"ws://127.0.0.1:{WS_PORT}"
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def rpc_call(method: str, params, timeout: float = 25.0) -> str:
    """One hello + one call against a candidate RPC endpoint."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((RPC_HOST, RPC_PORT))
    except OSError as exc:
        return f"CONNECT FAILED: {exc}"
    try:
        s.sendall((json.dumps({"type": "hello", "token": RPC_TOKEN}) + "\n").encode())
        line = s.makefile("rb").readline()
        if not line:
            return "no welcome (server closed)"
        welcome = line.decode("utf-8", "replace").strip()
        if '"welcome"' not in welcome:
            return f"unexpected welcome: {welcome[:200]}"
        req = {"id": 1, "method": method, "params": params}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        resp = s.makefile("rb").readline()
        return f"welcome={welcome[:80]} | resp={resp.decode('utf-8','replace').strip()[:1500]}"
    except OSError as exc:
        return f"RPC FAILED: {exc}"
    finally:
        s.close()


def probe_ports() -> None:
    """Look for any new loopback listener while the engine runs."""
    seen: set[int] = set()
    for _ in range(20):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
                 "Where-Object {$_.LocalAddress -eq '127.0.0.1'} | "
                 "Select-Object -ExpandProperty LocalPort"],
                capture_output=True, text=True, timeout=20,
            ).stdout.split()
            ports = {int(p) for p in out if p.isdigit()}
        except Exception:
            ports = set()
        new = ports - seen
        if new and seen:
            log(f"new loopback listeners: {sorted(new)}")
        seen |= ports
        time.sleep(1.0)
    log(f"all loopback listeners seen: {sorted(seen)}")


def main() -> int:
    stage()

    stub = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    env = os.environ.copy()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    # The knobs the engine looks for.
    env["WECOMAGENT_SANDBOX_CLI_PATH"] = SANDBOX_CLI
    env["WECOMAGENT_WECOM_BIN"] = WECOM_BIN
    env["WECOMAGENT_RPC_HOST"] = RPC_HOST
    env["WECOMAGENT_RPC_PORT"] = str(RPC_PORT)
    env["WECOMAGENT_RPC_TOKEN"] = RPC_TOKEN
    env["RUST_LOG"] = "info,wecom_agent::runtime::client_shell=debug,wecom_agent::runtime::python_host=debug"

    logpath = os.path.join(SCRATCH, "engine.log")
    fh = open(logpath, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([ENGINE], cwd=SCRATCH, env=env, stdout=fh, stderr=subprocess.STDOUT)
    log(f"engine pid={proc.pid}")

    watcher = threading.Thread(target=probe_ports, daemon=True)
    watcher.start()

    try:
        time.sleep(12)
        log(f"--- probe our chosen RPC port {RPC_PORT} ---")
        for method, params in (
            ("wecom.exec", {"argv": ["contact", "users", "search",
                                     "--json", '{"keywords":["test"]}']}),
            ("memory.search", {"query": "test"}),
        ):
            log(f"{method} -> {rpc_call(method, params)[:600]}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()
        stub.shutdown()

    print("\n=== engine log: client_shell / rpc / sandbox ===")
    keys = ("client_shell", "rpc", "sandbox-cli", "Client Shell", "python_host",
            "bind", "listener", "WECOMAGENT", "unavailable")
    try:
        with open(logpath, encoding="utf-8", errors="replace") as f2:
            for line in f2:
                if any(k.lower() in line.lower() for k in keys):
                    sys.stdout.buffer.write(b"  " + line.rstrip("\n").encode("utf-8", "replace")[:320] + b"\n")
    except OSError as exc:
        print("  log read failed:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
