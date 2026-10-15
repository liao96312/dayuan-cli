#!/usr/bin/env python3
"""Dump strings around the wecom CLI cli/command crate markers."""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"


def main() -> int:
    data = open(EXE, "rb").read()
    for pat in (rb"vendor\weclaw-cli\crates\wecom\src\cli",
                rb"vendor\weclaw-cli\crates\wecom\src\command"):
        hits = list(re.finditer(re.escape(pat), data))
        print(f"\n{'='*95}\n### {pat.decode()!r} ({len(hits)})")
        for m in hits[:4]:
            s = max(0, m.start() - 1800)
            e = min(len(data), m.end() + 1800)
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
            print(f"\n--- @ {m.start()} ---")
            print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
