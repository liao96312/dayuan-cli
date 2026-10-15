#!/usr/bin/env python3
"""Extract CLI / RPC / shell strings from wecom-agent.exe with context.

Goal: find the intended headless entry (subcommands, RPC server startup,
sandbox-cli wiring) so dayuan-cli can drive 大圆 without the UI.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"

PATTERNS = [
    rb"rpc_port",
    rb"agent RPC disabled",
    rb"shell wecom RPC capability prepared",
    rb"WECOMAGENT_SANDBOX_CLI_PATH",
    rb"agent-argv-env",
    rb"wecom: ",
    rb"Client Shell",
    rb"wecom.exe",
]


def strings_with_offsets(data: bytes, minlen: int = 6):
    """Yield (offset, text) for printable ASCII runs."""
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, data):
        yield m.start(), m.group().decode("ascii", "replace")


def main() -> None:
    data = open(EXE, "rb").read()
    print(f"size={len(data)}")

    # 1. Dedicated string dump around each pattern.
    for pat in PATTERNS:
        hits = list(re.finditer(pat, data))
        print(f"\n{'='*100}\n### {pat!r}  ({len(hits)} hits)")
        for m in hits[:3]:
            s = max(0, m.start() - 700)
            e = min(len(data), m.end() + 700)
            chunk = data[s:e]
            txt = "".join(chr(c) if 32 <= c < 127 else "\u00b7" for c in chunk)
            print(f"\n--- @ {m.start()} ---")
            print(txt)

    # 2. Collect short strings that look like CLI subcommands / flags.
    print(f"\n{'='*100}\n### flag-like strings")
    flags = set()
    for _, t in strings_with_offsets(data, 4):
        if re.fullmatch(r"--[a-z][a-z0-9-]{1,32}", t):
            flags.add(t)
    for f in sorted(flags):
        print("  ", f)

    print(f"\n{'='*100}\n### subcommand-like strings (verb list)")
    subs = set()
    for _, t in strings_with_offsets(data, 3):
        if re.fullmatch(r"[a-z][a-z0-9_]{2,20}", t):
            subs.add(t)
    interesting = sorted(s for s in subs if s in {
        "send", "search", "list", "get", "run", "exec", "shell", "cli",
        "message", "contact", "chat", "media", "agent", "serve", "daemon",
        "login", "logout", "status", "ask", "prompt", "session", "sessions",
    })
    print("  ", interesting)


if __name__ == "__main__":
    sys.exit(main())
