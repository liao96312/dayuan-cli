#!/usr/bin/env python3
"""Call tools on the public WeCom MCP bot endpoint.

Usage:
    python dev/mcp_call.py --tool chat_groups_list \
        --args '{"begin_time":"2026-10-08 00:00:00","end_time":"2026-10-15 23:59:59"}'
    python dev/mcp_call.py --tool message_send \
        --args '{"chat_id":"...","msg_type":"text","text":{"content":"hi"}}'
    python dev/mcp_call.py --list          # just dump full tools/list
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


def post(url: str, payload: dict, timeout: float = 60.0) -> tuple[int, str, dict | None]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "User-Agent": "dayuan-cli/1.0"},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        ctype = resp.headers.get("Content-Type", "")
        data = resp.read().decode("utf-8", "replace")
        return resp.status, data, parse(data, ctype)
    except urllib.error.HTTPError as e:
        ctype = e.headers.get("Content-Type", "")
        data = e.read().decode("utf-8", "replace")
        return e.code, data, parse(data, ctype)


def parse(data: str, ctype: str) -> dict | None:
    if "text/event-stream" in ctype:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--tool")
    ap.add_argument("--args", default="{}")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    url = f"{args.url}?apikey={load_apikey(args.apikey)}"

    # 1) initialize (stateless-ish server: no session id observed)
    code, raw, parsed = post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "dayuan-cli", "version": "1.0.0"}}})
    if code != 200 or parsed is None or "result" not in parsed:
        print(f"initialize failed HTTP {code}: {raw[:400]}")
        sys.exit(2)
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    # 2) tools/list
    code, raw, parsed = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                   "params": {}})
    tools = []
    if parsed and "result" in parsed:
        tools = parsed["result"].get("tools", [])
    if args.list:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return
    names = [t.get("name") for t in tools]
    print(f"[tools available] {names}", file=sys.stderr)

    if not args.tool:
        print("no --tool given and no --list; nothing to do", file=sys.stderr)
        sys.exit(0)
    if args.tool not in names:
        print(f"unknown tool {args.tool!r}; available: {names}", file=sys.stderr)
        sys.exit(2)

    # 3) tools/call
    try:
        call_args = json.loads(args.args)
    except json.JSONDecodeError as e:
        print(f"--args is not valid JSON: {e}", file=sys.stderr)
        sys.exit(2)
    code, raw, parsed = post(url, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": args.tool, "arguments": call_args}})
    print(f"HTTP {code}")
    if parsed is not None:
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    else:
        print(raw[:2000])


if __name__ == "__main__":
    main()
