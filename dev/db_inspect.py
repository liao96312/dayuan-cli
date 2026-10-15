#!/usr/bin/env python3
"""Inspect the live engine's SQLite stores (read-only copies).

The live engine executes authorized cloud calls. If scheduled tasks
(cron) live in a local store, injecting one would let the LIVE engine
send a message with no UI and no token of our own.

Copies db + -wal + -shm so the WAL contents are visible.

    python dev/db_inspect.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys

# WXWork per-account data dir; set DAYUAN_WXWORK_DATA_DIR to your own.
WXWORK_DATA_DIR = os.environ.get("DAYUAN_WXWORK_DATA_DIR", r"D:\文档\WXWork\<corp_id>")
LIVE = os.path.join(WXWORK_DATA_DIR, "WeComAgent", "WeComAgent")
DST = r"D:\dayuan-cli\.tmp-db2"

SOURCES = [
    (os.path.join("task_status", "task_status.db"), "task_status"),
    (os.path.join("osslog", "osslog.db"), "osslog"),
    (os.path.join("mcp-search", "index-postings-v1.db"), "mcp-search"),
]


def stage() -> list[str]:
    if os.path.isdir(DST):
        shutil.rmtree(DST, ignore_errors=True)
    os.makedirs(DST, exist_ok=True)
    made = []
    for rel, tag in SOURCES:
        for suffix in ("", "-wal", "-shm"):
            src = os.path.join(LIVE, rel + suffix)
            if os.path.isfile(src):
                dst = os.path.join(DST, tag + ".db" + suffix)
                shutil.copy2(src, dst)
        made.append(os.path.join(DST, tag + ".db"))
    return made


def dump(path: str) -> None:
    if not os.path.isfile(path):
        print(f"--- {path}: missing ---")
        return
    print(f"\n{'='*80}\n### {os.path.basename(path)} ({os.path.getsize(path)} bytes)")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print("  open failed:", exc)
        return
    try:
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print("  tables:", tables)
        for t in tables:
            try:
                n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error as exc:
                print(f"  [{t}] count failed: {exc}")
                continue
            print(f"\n  --- {t} ({n} rows) ---")
            try:
                cols = [d[0] for d in cur.execute(f'SELECT * FROM "{t}" LIMIT 1').description]
                print("      cols:", cols)
            except sqlite3.Error:
                pass
            try:
                for row in cur.execute(f'SELECT * FROM "{t}" LIMIT 8').fetchall():
                    text = str(row)
                    print("      ", text[:400])
            except sqlite3.Error as exc:
                print("      read failed:", exc)
    finally:
        con.close()


def main() -> int:
    made = stage()
    for p in made:
        dump(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
