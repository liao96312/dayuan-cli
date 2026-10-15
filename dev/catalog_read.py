#!/usr/bin/env python3
"""Interrogate the live CLI's own cached catalog + schema command.

The engine caches the discovery catalog at
<workspace>/default/cli/default/cache/catalog.json. If the CLI builds its
clap tree from that file, then planting a catalog and running the CLI
should expose real subcommands.

We drive the engine's exec tool with commands that only READ the catalog,
so nothing is sent to any user.

    python dev/catalog_read.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shim_drive import LlmShim, SHIM_PORT, launch_engine, run_session  # noqa: E402
import shim_drive

CATALOG_PATH = os.environ.get(
    "DAYUAN_CATALOG",
    r"D:\dayuan-cli\.tmp-fullenv\.weagent\default\cli\default\cache\catalog.json")


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmds", nargs="*", default=[
        "wecom help", "wecom --help", "wecom schema"])
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    # One tool call per command, so we can see each reply distinctly.
    steps = [{"kind": "tool", "name": "exec",
              "args": {"command": c, "execution_purpose": "读取 CLI 帮助信息"}}
             for c in args.cmds]
    steps.append({"kind": "text", "text": "HELP-DONE"})
    shim_drive.SCRIPT = shim_drive.Script(steps)

    shim = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), LlmShim)
    threading.Thread(target=shim.serve_forever, daemon=True).start()

    proc, fh = launch_engine(None)
    try:
        state = asyncio.run(run_session("看一下 CLI 支持哪些命令", args.timeout))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        fh.close()
        shim.shutdown()

    print("\n=== results ===")
    txt = os.path.join(r"D:\dayuan-cli\state", "shim-results.txt")
    try:
        with open(txt, encoding="utf-8") as f2:
            print(f2.read()[-6000:])
    except OSError as exc:
        print("no results:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
