"""量一下窗口地标：白色输入框的实际范围、发送键相对输入框的偏移。

用实测的基准图 + 当前活窗口，确认"从像素认地标"这条路可行。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dayuan_watch as dw  # noqa: E402


def px(bgra, w, x, y):
    i = (y * w + x) * 4
    return bgra[i + 2], bgra[i + 1], bgra[i]


def is_white(p) -> bool:
    return p[0] >= 248 and p[1] >= 248 and p[2] >= 248


def longest_white_run(bgra, w, y, x_from=0, x_to=None):
    x_to = x_to if x_to is not None else w
    best = (0, 0, 0)
    run_start = None
    for x in range(x_from, x_to):
        if is_white(px(bgra, w, x, y)):
            if run_start is None:
                run_start = x
        else:
            if run_start is not None:
                if x - run_start > best[0]:
                    best = (x - run_start, run_start, x - 1)
                run_start = None
    if run_start is not None and x_to - run_start > best[0]:
        best = (x_to - run_start, run_start, x_to - 1)
    return best


def find_input_box(bgra, w, h):
    """窗口下部找"很多行都很白、且白得很宽"的那一块 = 输入框。"""
    rows = []
    for y in range(int(h * 0.70), h):
        n, x0, x1 = longest_white_run(bgra, w, y)
        rows.append((y, n, x0, x1))
    wide = [(y, n, x0, x1) for (y, n, x0, x1) in rows if n >= 0.60 * w]
    if not wide:
        return None
    # 取最长连续块
    best = cur = [wide[0]]
    for r in wide[1:]:
        if r[0] == cur[-1][0] + 1:
            cur.append(r)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [r]
    if len(cur) > len(best):
        best = cur
    y0, y1 = best[0][0], best[-1][0]
    x0 = min(r[2] for r in best)
    x1 = max(r[3] for r in best)
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "w": x1 - x0 + 1, "h": y1 - y0 + 1}


def find_send_button(bgra, w, box):
    """输入框内找浅蓝圆角块（发送键）。忙碌时中心还有一块实心蓝。"""
    xs, ys = [], []
    for y in range(box["y0"], box["y1"] + 1):
        for x in range(box["x0"], box["x1"] + 1):
            r, g, b = px(bgra, w, x, y)
            # 浅蓝底：蓝明显高于红，且偏亮
            if b > 200 and b - r > 18 and g > r:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "cx": (min(xs) + max(xs)) // 2, "cy": (min(ys) + max(ys)) // 2}


def report(tag, bgra, w, h):
    print(f"=== {tag}  {w}x{h} ===")
    box = find_input_box(bgra, w, h)
    if not box:
        print("  输入框: 没找到")
        return
    print(f"  输入框: ({box['x0']},{box['y0']})-({box['x1']},{box['y1']}) {box['w']}x{box['h']}")
    print(f"    比例: x0/w={box['x0']/w:.3f} y0/h={box['y0']/h:.3f} "
          f"x1/w={box['x1']/w:.3f} y1/h={box['y1']/h:.3f}")
    btn = find_send_button(bgra, w, box)
    if btn:
        print(f"  发送键: ({btn['x0']},{btn['y0']})-({btn['x1']},{btn['y1']}) 中心=({btn['cx']},{btn['cy']})")
        print(f"    相对输入框右下角: 右 {box['x1']-btn['cx']}px, 下 {box['y1']-btn['cy']}px")
        print(f"    相对输入框左上角: +{btn['cx']-box['x0']}px, +{btn['cy']-box['y0']}px")
    else:
        print("  发送键: 没找到（可能是空闲态的浅蓝没达标）")


def main() -> int:
    base = Path(__file__).resolve().parent.parent / "state"
    for name in ("ref_idle.bmp", "ref_consent.bmp"):
        p = base / name
        if p.exists():
            w, h, bgra = dw.load_bmp(p)
            report(name, bgra, w, h)

    hwnd = dw.find_window()
    if hwnd:
        w, h, bgra = dw.grab(hwnd)
        report(f"live hwnd={hwnd}", bgra, w, h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
