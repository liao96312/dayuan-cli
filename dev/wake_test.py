"""唤醒实验：还原企微主窗口，抓图看「智能助理」的入口在哪。"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import dayuan_watch as dw  # noqa: E402

user32 = ctypes.windll.user32
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL

SW_RESTORE = 9
SW_SHOW = 5
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def main() -> int:
    for title in ("企业微信", "智能助理"):
        hwnd = dw.find_window(title)
        if hwnd is None:
            print(f"「{title}」: 没找到")
            continue
        left, top, w, h = dw.window_rect(hwnd)
        print(f"「{title}」: hwnd={hwnd} iconic={bool(user32.IsIconic(hwnd))} "
              f"rect=({left},{top}) {w}x{h}")

    main_hwnd = dw.find_window("企业微信")
    if main_hwnd is None:
        print("主窗口都没找到，退出")
        return 2

    print("\n--- 尝试 SW_RESTORE ---")
    user32.ShowWindow(main_hwnd, SW_RESTORE)
    time.sleep(0.8)
    print(f"iconic={bool(user32.IsIconic(main_hwnd))} rect={dw.window_rect(main_hwnd)}")

    if user32.IsIconic(main_hwnd):
        print("还是最小化，试 ShowWindow(SW_SHOW) + SetWindowPos")
        user32.ShowWindow(main_hwnd, SW_SHOW)
        user32.SetWindowPos(main_hwnd, None, 60, 60, 1120, 700,
                            SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        time.sleep(0.8)
        print(f"iconic={bool(user32.IsIconic(main_hwnd))} rect={dw.window_rect(main_hwnd)}")

    out = Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    try:
        w, h, bgra = dw.grab(main_hwnd)
        Image.frombytes("RGBA", (w, h), bgra, "raw", "BGRA").convert("RGB").save(out / "mainwin.png")
        print(f"\n主窗口已抓图 {w}x{h} -> dev\\mainwin.png")
    except Exception as exc:  # noqa: BLE001
        print(f"抓图失败: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
