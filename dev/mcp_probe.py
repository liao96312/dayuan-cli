#!/usr/bin/env python3
"""Probe the public WeCom MCP bot endpoint found in WXWork memory.

Endpoint (from the WorkBuddy Agent Card message):
    https://qyapi.weixin.qq.com/mcp/v2/bot/msg?apikey=<key>

Transport per the demo config: MCP "streamable-http" (JSON-RPC 2.0 over
HTTP POST; responses may be JSON or SSE stream).

This script only performs the protocol handshake (initialize +
notifications/initialized + tools/list). It never calls tools, so it
has no side effects.

    python dev/mcp_probe.py [--url URL] [--apikey KEY]
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.request
import urllib.error

DEFAULT_URL = "https://qyapi.weixin.qq.com/mcp/v2/bot/msg"


def load_apikey(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("DAYUAN_MCP_APIKEY")
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "state", "mcp_apikey.txt")
    if os.path.exists(path):
        v = open(path, encoding="utf-8").read().strip()
        if v:
            return v
    sys.exit("missing apikey: set DAYUAN_MCP_APIKEY or state/mcp_apikey.txt")

PROTOCOL_VERSION = "2025-03-26"


class McpClient:
    def __init__(self, url: str, timeout: float = 30.0, insecure: bool = False):
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self.req_id = 0
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.raw_log: list[str] = []

    def post(self, payload: dict) -> tuple[int, dict | None, str]:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "dayuan-cli/1.0",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read().decode("utf-8", "replace")
            self.raw_log.append(f"-> {payload.get('method')}  <- HTTP {resp.status} "
                                f"ct={ctype} len={len(data)} sid={sid}")
            parsed = self._parse(data, ctype)
            return resp.status, parsed, data
        except urllib.error.HTTPError as e:
            data = e.read().decode("utf-8", "replace")
            self.raw_log.append(f"-> {payload.get('method')}  <- HTTP {e.code} "
                                f"ct={e.headers.get('Content-Type')} len={len(data)}")
            parsed = self._parse(data, e.headers.get("Content-Type", ""))
            return e.code, parsed, data

    @staticmethod
    def _parse(data: str, ctype: str) -> dict | None:
        if "text/event-stream" in ctype:
            # SSE: take every data: line, return list of events
            events = []
            for line in data.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            events.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            events.append({"_raw": chunk})
            return {"_sse": events} if events else None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    def rpc(self, method: str, params: dict | None = None) -> dict | None:
        self.req_id += 1
        payload = {"jsonrpc": "2.0", "id": self.req_id, "method": method}
        if params is not None:
            payload["params"] = params
        _, parsed, raw = self.post(payload)
        if parsed is None:
            print(f"[raw] {raw[:400]}")
        return parsed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    cli = McpClient(f"{args.url}?apikey={load_apikey(args.apikey)}", args.timeout, args.insecure)

    print("=== initialize ===")
    r = cli.rpc("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "dayuan-cli", "version": "1.0.0"},
    })
    print(json.dumps(r, ensure_ascii=False, indent=2)[:3000] if r else "(no parseable response)")

    if r and not r.get("_sse") and "error" in (r or {}):
        print("initialize returned error; stopping")
        for line in cli.raw_log:
            print("  " + line)
        sys.exit(2)

    print("\nsession id:", cli.session_id)

    print("\n=== notifications/initialized ===")
    cli.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    print("\n=== tools/list ===")
    r = cli.rpc("tools/list", {})
    print(json.dumps(r, ensure_ascii=False, indent=2)[:6000] if r else "(no parseable response)")

    print("\n=== protocol log ===")
    for line in cli.raw_log:
        print("  " + line)


if __name__ == "__main__":
    main()
