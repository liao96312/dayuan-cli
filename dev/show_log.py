#!/usr/bin/env python3
"""Print full engine log lines matching a pattern (no truncation)."""
from __future__ import annotations

import sys

LOG = r"D:\dayuan-cli\.tmp-cloudtest\engine.log"


def main() -> None:
    pat = sys.argv[1] if len(sys.argv) > 1 else "reqwest request"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    shown = 0
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if pat in line:
                sys.stdout.buffer.write(line.rstrip("\n").encode("utf-8", "replace") + b"\n\n")
                shown += 1
                if shown >= limit:
                    break


if __name__ == "__main__":
    main()
