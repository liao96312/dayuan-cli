#!/usr/bin/env python3
"""Find the MCP transport aliases the CLI understands.

Binary strings show `mcp://wecom_tool` and `mcp://direct`. The CLI's
first call (service.discovery) is failing with "Missing 'result' field",
so it may expect a *different* envelope than the tools/call wrapper.

This prints every occurrence with context so we can see the expected
request/response pairs.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"
NEEDLES = [b"mcp://direct", b"wecom_tool", b"mcp://wecom_tool", b"service_discovery",
           b"service-discovery", b"Missing 'text' content from MCP response"]


def show(data: bytes, needle: bytes, before: int, after: int, limit: int) -> None:
    hits = list(re.finditer(re.escape(needle), data))
    print(f"\n{'='*90}\n### {needle!r} ({len(hits)} hits)")
    for m in hits[:limit]:
        s = max(0, m.start() - before)
        e = min(len(data), m.end() + after)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(f"\n--- @ {m.start()} ---")
        print(txt)


def main() -> int:
    data = open(EXE, "rb").read()
    for n in NEEDLES:
        show(data, n, 400, 400, 2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
