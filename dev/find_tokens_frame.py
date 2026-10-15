#!/usr/bin/env python3
"""Find the control-channel frame that delivers TokensConfig.

Key strings from the binary:
  struct variant ClientHandshake::Hello { task media_ids urls token
                                          websocket_url llm_access_token }
  TokensConfig { access_token expired_at llm_expired_at
                 refresh_token refresh_token_expired_at }
  AgentApp::update_tokens ... LLM api_key updated / wecom access_token updated
  "applying agent-client-config.json global_override"
  "injected llm_access_token into provider api_key"

So the WS peer (us) is expected to send a token/hello frame. We never
did, which is why our engine had no cloud credentials.

This extracts the surrounding strings so we can build the right frame.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"

NEEDLES = [
    b"ClientHandshake::Hello",
    b"llm_access_token",
    b"update_tokens",
    b"injected llm_access_token into provider api_key",
    b"local_wecomagent",
    b"TokensConfig",
]


def ctx(data: bytes, needle: bytes, before: int, after: int, limit: int = 2) -> None:
    hits = list(re.finditer(re.escape(needle), data))
    print(f"\n{'='*95}\n### {needle.decode('utf-8','replace')!r} ({len(hits)} hits)")
    for m in hits[:limit]:
        s = max(0, m.start() - before)
        e = min(len(data), m.end() + after)
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
        print(f"\n--- @ {m.start()} ---")
        print(txt)


def main() -> int:
    data = open(EXE, "rb").read()
    for n in NEEDLES:
        ctx(data, n, 600, 700)
    return 0


if __name__ == "__main__":
    sys.exit(main())
