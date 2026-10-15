#!/usr/bin/env python3
"""Find the discovery response `items` schema in the CLI binary.

The CLI now says: "Failed to deserialize discovery response: missing
field `items`" at endpoint //discovery?service_name=<catalog>.

So the payload needs result.items[]. Let's find the struct's field list.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"


def ctx(data: bytes, needle: bytes, before: int, after: int) -> None:
    hits = list(re.finditer(re.escape(needle), data))
    print(f"\n{'='*90}\n### {needle!r} ({len(hits)} hits)")
    for m in hits[:4]:
        s = max(0, m.start() - before)
        e = min(len(data), m.end() + after)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(f"\n--- @ {m.start()} ---")
        print(txt)


def main() -> int:
    data = open(EXE, "rb").read()
    for n in [b"discovery response", b"service_name=<catalog>",
              b"//discovery?service_name=", b"DiscoveredService"]:
        ctx(data, n, 700, 900)
    return 0


if __name__ == "__main__":
    sys.exit(main())
