#!/usr/bin/env python3
"""Find how the engine authorizes cloud (cloud CGI) calls.

The engine logs has_authorization=false for our standalone instance but
the live one works. This locates the strings/env that supply the token.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"

PATTERNS = [
    rb"has_authorization",
    rb"authorization",
    rb"Authorization",
    rb"access_token",
    rb"api_key",
    rb"/cgi-bin/claw",
    rb"approvalid",
    rb"WECLAW_",
    rb"WECOMAGENT_",
]


def main() -> None:
    data = open(EXE, "rb").read()
    print(f"size={len(data)}\n")

    # Env-var-looking names (upper snake case) - these are the knobs.
    print("=" * 90)
    print("### WECLAW_ / WECOMAGENT_ env names")
    names = set()
    for m in re.finditer(rb"[A-Z][A-Z0-9_]{5,40}", data):
        t = m.group().decode()
        if t.startswith(("WECLAW", "WECOMAGENT", "WECOM_", "WXWORK")):
            names.add(t)
    for n in sorted(names):
        print("  ", n)

    print("\n" + "=" * 90)
    print("### context around 'has_authorization' / 'authorization'")
    for pat in (rb"has_authorization", rb"approvalid", rb"/cgi-bin/claw"):
        hits = list(re.finditer(pat, data))
        print(f"\n--- {pat!r}: {len(hits)} hits ---")
        for m in hits[:2]:
            s = max(0, m.start() - 900)
            e = min(len(data), m.end() + 900)
            txt = "".join(chr(c) if 32 <= c < 127 else "\u00b7" for c in data[s:e])
            print(f"@ {m.start()}:")
            print(txt)
            print()

    print("=" * 90)
    print("### strings mentioning token/credential sources")
    for m in re.finditer(rb"[\x20-\x7e]{12,}", data):
        t = m.group().decode("ascii", "replace")
        if re.search(r"(token|credential|auth)", t, re.I) and re.search(
                r"(env|variable|header|missing|required|refresh|load|read)", t, re.I):
            print("  ", t[:160])


if __name__ == "__main__":
    sys.exit(main())
