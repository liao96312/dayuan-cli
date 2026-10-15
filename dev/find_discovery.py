#!/usr/bin/env python3
"""Locate the expected service.discovery response struct in the binary.

The CLI rejects our reply with "Missing 'result' field in final response"
at mcp://service.discovery. It also complained about missing 'text'
content when we returned a content block alone. So the expected envelope
has both. This finds the struct by locating the parse-error strings and
reading the field names around them.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"

NEEDLES = [
    b"Missing 'result' field in final response",
    b"Missing 'text' content from MCP response",
    b"service.discovery",
    b"discovery",
]


def ctx(data: bytes, needle: bytes, before: int, after: int) -> None:
    for m in list(re.finditer(re.escape(needle), data))[:3]:
        s = max(0, m.start() - before)
        e = min(len(data), m.end() + after)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(f"\n--- {needle[:40]!r} @ {m.start()} ---")
        print(txt)


def main() -> int:
    data = open(EXE, "rb").read()
    print(f"size={len(data)}")
    for n in NEEDLES:
        print(f"\n{'='*90}\n### {n!r}: "
              f"{len(list(re.finditer(re.escape(n), data)))} hits")
    ctx(data, b"Missing 'result' field in final response", 900, 900)
    return 0


if __name__ == "__main__":
    sys.exit(main())
