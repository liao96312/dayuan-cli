#!/usr/bin/env python3
"""Extract the wecom CLI's clap subcommand names.

The CLI is `wecom [COMMAND]` and the discovery items populate it. Look
for the clap Command definitions / subcommand literals next to the CLI
crate's other strings.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"

# Subcommands we know exist from the SKILL docs.
KNOWN = [b"contact", b"message", b"chat", b"media", b"cron", b"agent",
         b"doc", b"sheet", b"search", b"calendar", b"meeting", b"todo",
         b"email", b"approval", b"skill", b"pptx", b"summary", b"smartsheet"]

# clap builds its command list from a `Commands` enum; the enum variant
# names are stored as short ASCII strings. Print short strings that look
# like CLI tops and their frequencies.


def main() -> int:
    data = open(EXE, "rb").read()
    print("=== count of known subcommand literals ===")
    for k in KNOWN:
        n = len(re.findall(rb"(?<![A-Za-z])" + re.escape(k) + rb"(?![A-Za-z])", data))
        print(f"  {k.decode():<12} {n}")

    # Dump the ASCII string table region of the wecom CLI crate: strings
    # between the crate path markers.
    markers = [b"vendor\\weclaw-cli\\crates\\wecom\\src\\main.rs",
               b"vendor\\weclaw-cli\\crates\\wecom\\src\\cli",
               b"vendor\\weclaw-cli\\crates\\wecom\\src\\command"]
    print("\n=== crate markers ===")
    for m in markers:
        print(f"  {m!r}: {len(re.findall(re.escape(m), data))}")

    # Any short lowercase tokens near 'Usage: wecom'
    print("\n=== around 'Usage: wecom' ===")
    for m in list(re.finditer(rb"Usage: wecom", data))[:3]:
        s = max(0, m.start() - 2500)
        e = min(len(data), m.end() + 2500)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(txt, "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
