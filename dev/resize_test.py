"""坐标鲁棒性实测：把窗口挪走、放大、缩小，看地标是不是跟着走。

这是"坐标一变就废"那个隐患的回归测试。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dayuan_watch as dw  # noqa: E402

user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def show(tag, hwnd):
    l, t, w, h = dw.window_rect(hwnd)
    gw, gh, shot = dw.grab(hwnd)
    m = dw.landmarks(shot, gw, gh)
    box = m["box"]
    print(f"  {tag}")
    print(f"    窗口 ({l},{t}) {w}x{h}   抓图 {gw}x{gh}")
    print(f"    地标 source={m['source']}  box={box}")
    print(f"    input={m['input']}  send={m['send']}  ocr={m['scan_top']}..{m['scan_bottom']}")
    # 用旧的比例法算一遍做对照，看差多少
    old_in = (int(gw * dw.INPUT_X), int(gh * dw.INPUT_Y))
    old_sd = (int(gw * dw.SEND_X), int(gh * dw.SEND_Y))
    print(f"    旧比例法会是 input={old_in} send={old_sd}"
          f"   → 偏差 input={abs(old_in[0]-m['input'][0])},{abs(old_in[1]-m['input'][1])}px"
          f" send={abs(old_sd[0]-m['send'][0])},{abs(old_sd[1]-m['send'][1])}px")
    return m


def main() -> int:
    hwnd = dw.wake()
    if hwnd is None:
        print("窗口叫不出来")
        return 2
    orig = dw.window_rect(hwnd)
    print(f"原始: ({orig[0]},{orig[1]}) {orig[2]}x{orig[3]}\n")

    try:
        for tag, x, y, w, h in (
            ("① 只挪位置（不改大小）", 400, 300, orig[2], orig[3]),
            ("② 放大到 760x900", 500, 80, 760, 900),
            ("③ 缩到 420x560", 300, 200, 420, 560),
        ):
            print(f"=== {tag} ===")
            user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(1.2)
            show(tag, hwnd)
            print()
    finally:
        print("=== 恢复原尺寸 ===")
        user32.SetWindowPos(hwnd, None, orig[0], orig[1], orig[2], orig[3],
                            SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(1.0)
        show("恢复后", hwnd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
