#!/usr/bin/env python3
"""Compare wecom-agent.exe and wecom.exe and locate CLI/RPC wiring strings."""
from __future__ import annotations

import hashlib
import re

A = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"
B = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom.exe"


def sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    da = open(A, "rb").read()
    db = open(B, "rb").read()
    print("agent size", len(da), "sha", sha(A))
    print("wecom size", len(db), "sha", sha(B))
    print("identical:", da == db)

    needles = [
        b"WECOMAGENT_SANDBOX_CLI_PATH",
        b"WECOMAGENT_RPC_HOST",
        b"WECOMAGENT_RPC_PORT",
        b"WECOMAGENT_RPC_TOKEN",
        b"WECOMAGENT_WECOM_BIN",
        b"agent-client-config.json",
        b"Client Shell",
        b"client_shell",
        b"sandbox-cli",
        b"wecom.exe",
        b"message send",
        b"contact users search",
        b"wecom message",
        b"agent-argv-env",
        b"--agent-argv-env",
        b"rpc_port",
        b"safe-delete-rpc",
        b"python_tool",
    ]
    print(f"\n{'needle':<34} {'agent':>7} {'wecom':>7}")
    for n in needles:
        print(f"{n.decode('utf-8','replace'):<34} {da.count(n):>7} {db.count(n):>7}")

    # Pull the ASCII strings around the sandbox-cli mention in the agent.
    print("\n=== context around 'Client Shell' / sandbox-cli in agent ===")
    for pat in (b"Client Shell", b"sandbox-cli", b"WECOMAGENT_SANDBOX_CLI_PATH"):
        for m in re.finditer(re.escape(pat), da):
            s = max(0, m.start() - 300)
            e = min(len(da), m.end() + 300)
            chunk = da[s:e]
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            print(f"\n-- {pat!r} @ {m.start()} --")
            print(txt)
            break


if __name__ == "__main__":
    main()
