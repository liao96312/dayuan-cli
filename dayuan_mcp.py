#!/usr/bin/env python3
"""大圆 MCP —— 企业微信「智能助理/大圆」Agent Card 的非 UI 调用 CLI。

背景（2026-10-15 调研结论，详见 NONUI-PROGRESS.md 第九节）：
  大圆展示的 Agent Card（agent_id: wecom-message-agent-3b3b9074）声明了
  A2A v1.0.0 端点 http://<internal-a2a-host>/cgi-bin/a2a/api，
  bearer 鉴权。实测该域名为 WorkBuddy 平台集群内部专用（系统 DNS、企微
  客户端、公网均不可达，连腾讯云沙箱里的大圆自己都解析不了），token 由
  内部网关注入，外部拿不到也不需要拿。

  同一张 Card 公开的对外通道是 MCP streamable-http：
      https://qyapi.weixin.qq.com/mcp/v2/bot/msg?apikey=<key>
  （即 Card 上"外部文档 URL https://dayuan.wecom.work/docs"对应的接入方式，
   在企微客户端内存的 mcpServers 演示配置中给出了完整 apikey。）
  鉴权即 URL 中的 apikey —— 对应 Agent Card 说的"Bearer 由网关层自动处理"：
  调用方代码里不出现任何明文 token。

本 CLI 用标准 MCP 协议（JSON-RPC 2.0 over HTTP POST）调用该端点：
  会话/消息的读、机器人身份的富媒体发送、以授权用户身份发文本。

用法：
  python dayuan_mcp.py tools                    # 服务器能力与工具清单
  python dayuan_mcp.py sessions                 # 机器人最近会话（含 chat_id）
  python dayuan_mcp.py groups --days 7          # 有消息的群会话（视企业灰度）
  python dayuan_mcp.py messages <chat_id> --days 1
  python dayuan_mcp.py send <chat_id> "你好"    # 以授权用户身份发文本（需确认）
  python dayuan_mcp.py send-aibot <chat_id> --markdown "# 标题"
  python dayuan_mcp.py files <media_id> --out out.bin

apikey 优先级：--apikey > 环境变量 DAYUAN_MCP_APIKEY > state/mcp_apikey.txt。
密钥不进仓库。state/ 已被 gitignore。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
DEFAULT_URL = "https://qyapi.weixin.qq.com/mcp/v2/bot/msg"
PROTOCOL_VERSION = "2025-03-26"
ERR_NOT_OPEN = 853006


def load_apikey(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("DAYUAN_MCP_APIKEY")
    if env:
        return env
    f = STATE_DIR / "mcp_apikey.txt"
    if f.exists():
        v = f.read_text(encoding="utf-8").strip()
        if v:
            return v
    print("[!] 没有 apikey。设置 DAYUAN_MCP_APIKEY，或写入 state/mcp_apikey.txt。",
          file=sys.stderr)
    sys.exit(2)


class Mcp:
    """Minimal MCP streamable-http client (JSON responses; SSE tolerated)."""

    def __init__(self, url: str, apikey: str, timeout: float = 60.0):
        self.url = f"{url}?apikey={apikey}"
        self.timeout = timeout
        self._id = 0

    def _post(self, payload: dict) -> tuple[int, str, dict | None]:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "User-Agent": "dayuan-cli/1.0"},
            method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read().decode("utf-8", "replace")
            return resp.status, data, self._parse(data, ctype)
        except urllib.error.HTTPError as e:
            ctype = e.headers.get("Content-Type", "")
            data = e.read().decode("utf-8", "replace")
            return e.code, data, self._parse(data, ctype)

    @staticmethod
    def _parse(data: str, ctype: str) -> dict | None:
        if "text/event-stream" in ctype:
            events = []
            for line in data.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            events.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            pass
            if events:
                return events[0] if len(events) == 1 else {"_sse": events}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    def _rpc(self, method: str, params: dict | None = None,
             notification: bool = False) -> dict | None:
        if not notification:
            self._id += 1
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._id
        if params is not None:
            payload["params"] = params
        _, raw, parsed = self._post(payload)
        if parsed is None and not notification and raw:
            print(f"[!] 非 JSON 响应（前 400 字节）：{raw[:400]}", file=sys.stderr)
        return parsed

    def hello(self) -> dict | None:
        r = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dayuan-cli", "version": "1.0.0"}})
        if r is None or "result" not in r:
            print(f"[!] initialize 失败：{json.dumps(r, ensure_ascii=False)[:300] if r else '无响应'}",
                  file=sys.stderr)
            sys.exit(2)
        self._rpc("notifications/initialized", notification=True)
        return r

    def tools(self) -> list[dict]:
        r = self._rpc("tools/list", {})
        return (r or {}).get("result", {}).get("tools", [])

    def call(self, name: str, arguments: dict) -> dict | None:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


def unwrap(parsed: dict | None) -> str:
    """MCP tool result -> inner text (content[0].text), else raw dump."""
    if parsed is None:
        return "(no response)"
    if "_sse" in parsed:
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    content = (parsed.get("result") or {}).get("content") or []
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    if texts:
        return "\n".join(texts)
    if "error" in parsed:
        return "jsonrpc error: " + json.dumps(parsed["error"], ensure_ascii=False)
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def pretty_inner(text: str) -> str:
    """Tool inner text is itself JSON — pretty-print when possible."""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return text


def note_errcode(inner: str) -> None:
    if '"errcode": %d' % ERR_NOT_OPEN in inner or '"errcode":%d' % ERR_NOT_OPEN in inner \
            or f'errcode": {ERR_NOT_OPEN}' in inner:
        print(f"[!] errcode {ERR_NOT_OPEN}：该工具未对本企业开放（平台灰度限制），协议层正常。",
              file=sys.stderr)


def fmt_range(days: int) -> tuple[str, str]:
    end = dt.datetime.now().replace(second=59, microsecond=0)
    begin = end - dt.timedelta(days=days)
    return (begin.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))


def confirm_send(target: str, body_desc: str, assume_yes: bool) -> bool:
    print(f"即将发送 →  {target}")
    print(f"内容      →  {body_desc}")
    if assume_yes:
        print("（--yes 已指定，跳过确认）")
        return True
    try:
        ans = input("确认发送？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--apikey", default=None,
                    help="默认：环境变量 DAYUAN_MCP_APIKEY 或 state/mcp_apikey.txt")
    ap.add_argument("--timeout", type=float, default=60.0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help="服务器信息与工具清单")
    sub.add_parser("sessions", help="机器人最近会话（含 chat_id）")

    g = sub.add_parser("groups", help="有消息的群会话列表")
    g.add_argument("--days", type=int, default=7, help="时间范围（天，上限 7）")

    m = sub.add_parser("messages", help="拉取指定会话的消息")
    m.add_argument("chat_id")
    m.add_argument("--days", type=int, default=1, help="时间范围（天，上限 7）")

    s = sub.add_parser("send", help="以授权用户身份发送文本消息")
    s.add_argument("chat_id", help="单聊传对方 userid，群聊传群会话 ID")
    s.add_argument("text")
    s.add_argument("--yes", action="store_true", help="跳过发送确认")

    sa = sub.add_parser("send-aibot", help="以机器人身份发送富媒体消息")
    sa.add_argument("chat_id")
    sa.add_argument("--markdown")
    sa.add_argument("--image", help="media_id（先经 file_upload，见 tools 说明）")
    sa.add_argument("--file", help="media_id")
    sa.add_argument("--voice", help="media_id")
    sa.add_argument("--video", help="media_id")
    sa.add_argument("--yes", action="store_true")

    f = sub.add_parser("files", help="按 media_id 获取文件内容")
    f.add_argument("media_id")
    f.add_argument("--out", default=None, help="保存路径（默认 state/files/<media_id>）")

    args = ap.parse_args()
    cli = Mcp(args.url, load_apikey(args.apikey), args.timeout)

    info = cli.hello()
    server = (info or {}).get("result", {}).get("serverInfo", {})
    print(f"[MCP] {server.get('name','?')} v{server.get('version','?')} "
          f"@ {args.url}", file=sys.stderr)

    if args.cmd == "tools":
        for t in cli.tools():
            print(f"- {t['name']}: {t.get('description','')}")
        return

    if args.cmd == "sessions":
        inner = unwrap(cli.call("message_aibot_sessions_list", {}))
        print(pretty_inner(inner))
        return

    if args.cmd == "groups":
        b, e = fmt_range(args.days)
        inner = unwrap(cli.call("chat_groups_list", {"begin_time": b, "end_time": e}))
        note_errcode(inner)
        print(pretty_inner(inner))
        return

    if args.cmd == "messages":
        b, e = fmt_range(args.days)
        inner = unwrap(cli.call("chat_messages_list",
                                {"chat_id": args.chat_id, "begin_time": b, "end_time": e}))
        note_errcode(inner)
        print(pretty_inner(inner))
        return

    if args.cmd == "send":
        if len(args.text) > 2048:
            print("[!] 文本超过 2048 字符上限", file=sys.stderr)
            sys.exit(2)
        if not confirm_send(args.chat_id, f"[text] {args.text}", args.yes):
            print("已取消。")
            return
        inner = unwrap(cli.call("message_send", {
            "chat_id": args.chat_id, "msg_type": "text",
            "text": {"content": args.text}}))
        print(pretty_inner(inner))
        return

    if args.cmd == "send-aibot":
        kinds = {k: v for k, v in (("markdown", args.markdown), ("image", args.image),
                                   ("file", args.file), ("voice", args.voice),
                                   ("video", args.video)) if v}
        if len(kinds) != 1:
            print("[!] 需要 --markdown/--image/--file/--voice/--video 恰好一项", file=sys.stderr)
            sys.exit(2)
        kind, val = next(iter(kinds.items()))
        content = {"content": val} if kind in ("markdown",) else {"media_id": val}
        if not confirm_send(args.chat_id, f"[{kind}] {val[:200]}", args.yes):
            print("已取消。")
            return
        inner = unwrap(cli.call("message_aibot_send", {
            "chat_id": args.chat_id, "msg_type": kind, kind: content}))
        print(pretty_inner(inner))
        return

    if args.cmd == "files":
        inner = unwrap(cli.call("message_files_get", {"media_id": args.media_id}))
        # 文件内容可能是 base64/引用形式，先展示结构
        print(pretty_inner(inner)[:4000])
        return


if __name__ == "__main__":
    main()
