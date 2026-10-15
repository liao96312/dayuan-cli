#!/usr/bin/env python3
"""Dump the serde struct registry for the discovery catalog.

The CLI accepts our catalog (it wrote catalog.json) but still says
"unrecognized subcommand". So the catalog must also carry the schemas it
uses to build the clap tree. Find every Service*/Method* field name.
"""
from __future__ import annotations

import re
import sys

EXE = r"D:\Program Files (x86)\WXWork\5.0.12.6018\WeComAgent\wecom-agent.exe"


def main() -> int:
    data = open(EXE, "rb").read()
    for needle in [b"ServiceCatalog", b"ServiceSchema", b"ServiceResource",
                   b"MethodSchema", b"ServiceInfo",
                   b"struct ServiceCatalog", b"struct ServiceSchema"]:
        print(f"{needle.decode():<28} {len(re.findall(re.escape(needle), data))}")

    m = re.search(rb"ServiceCatalog\.items", data)
    if not m:
        print("\nno ServiceCatalog.items anchor")
        return 1
    s = max(0, m.start() - 2200)
    e = min(len(data), m.end() + 2200)
    txt = "".join(chr(c) if 32 <= c < 127 else "." for c in data[s:e])
    print("\n=== registry window ===")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
