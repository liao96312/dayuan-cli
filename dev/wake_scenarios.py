"""唤醒能力实测：最小化 / 关闭 两种状态，看 wake() 能不能把它弄回来。"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dayuan_watch as dw  # noqa: E402

user32 = ctypes.windll.user32
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

SW_MINIMIZE = 6
WM_CLOSE = 0x0010


def state(tag: str, hwnd):
    if hwnd is None:
        print(f"  {tag}: 窗口不存在")
        return
    l, t, w, h = dw.window_rect(hwnd)
    print(f"  {tag}: hwnd={hwnd} rect=({l},{t}) {w}x{h} minimized={l < -10000}")


def main() -> int:
    print("=== 场景 1：最小化 -> wake ===")
    hwnd = dw.find_window()
    if hwnd is None:
        print("  当前没开，先 wake 一次")
        hwnd = dw.wake()
    state("最小化前", hwnd)
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(1.0)
    state("已最小化", dw.find_window())
    t0 = time.time()
    got = dw.wake()
    print(f"  wake() -> {got}  用时 {time.time()-t0:.1f}s")
    state("wake 之后", got)

    print("\n=== 场景 2：关闭 -> wake ===")
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(1.5)
    state("已关闭", dw.find_window())
    t0 = time.time()
    got = dw.wake()
    print(f"  wake() -> {got}  用时 {time.time()-t0:.1f}s")
    state("wake 之后", got)
    if got:
        w, h, shot = dw.grab(got)
        marks = dw.landmarks(shot, w, h)
        print(f"  地标: source={marks['source']} box={marks['box']}")
        print(f"        input={marks['input']} send={marks['send']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
