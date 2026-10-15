#!/usr/bin/env python3
"""Enumerate the wecom CLI's real subcommands from the binary.

With a working discovery answer the CLI now reaches its clap parser and
says "unrecognized subcommand 'contact'". So the discovery response must
tell it which subcommands exist. Let's read them out of the binary.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"


def main() -> int:
    data = open(EXE, "rb").read()
    # The CLI crate is vendor/weclaw-cli/crates/wecom. Its clap command
    # names live near its other strings.
    print("=== near 'unrecognized subcommand' ===")
    for m in list(re.finditer(rb"unrecognized subcommand", data))[:2]:
        s = max(0, m.start() - 1200)
        e = min(len(data), m.end() + 1200)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(txt, "\n")

    print("=== near 'vendor\\weclaw-cli\\crates\\wecom\\src\\registry\\mod.rs' ===")
    for m in list(re.finditer(re.escape(rb"vendor\weclaw-cli\crates\wecom\src\registry\mod.rs"), data))[:1]:
        s = max(0, m.start() - 3000)
        e = min(len(data), m.end() + 3000)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
