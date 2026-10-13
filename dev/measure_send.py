"""精确量发送键：在输入框右下角扫，找那块浅蓝圆角方块的边界。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dayuan_watch as dw  # noqa: E402


def px(bgra, w, x, y):
    i = (y * w + x) * 4
    return bgra[i + 2], bgra[i + 1], bgra[i]


def scan(name, bgra, w, h, box):
    print(f"=== {name}  输入框右下角 ({box['x1']},{box['y1']}) ===")
    # 在右下角 60x50 区域打出颜色网格（每 3px 采一次）
    x_from, x_to = box["x1"] - 58, box["x1"] + 1
    y_from, y_to = box["y1"] - 48, box["y1"] + 1
    print("      " + "".join(f"{x:>4}" for x in range(x_from, x_to, 6)))
    for y in range(y_from, y_to, 4):
        row = []
        for x in range(x_from, x_to, 6):
            r, g, b = px(bgra, w, x, y)
            if b - r > 20 and b > 230:
                tag = "蓝"
            elif r > 248 and g > 248 and b > 248:
                tag = "白"
            else:
                tag = "."
            row.append(f"{tag:>4}")
        print(f"  {y:>4}" + "".join(row))

    # 精确找浅蓝块的连通范围：只在输入框右下 1/3 区域，且要求成块
    xs, ys = [], []
    for y in range(box["y1"] - 60, box["y1"] + 1):
        for x in range(box["x1"] - 70, box["x1"] + 1):
            r, g, b = px(bgra, w, x, y)
            if b - r > 25 and b > 235 and r > 195 and g > r:
                xs.append(x)
                ys.append(y)
    if xs:
        cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
        print(f"  浅蓝块: x {min(xs)}..{max(xs)}  y {min(ys)}..{max(ys)}  "
              f"({max(xs)-min(xs)+1}x{max(ys)-min(ys)+1}) 共 {len(xs)} px")
        print(f"  中心=({cx},{cy})   相对输入框右下角: 右 {box['x1']-cx}px, 下 {box['y1']-cy}px")
    else:
        print("  没找到浅蓝块")


def main() -> int:
    for name in ("ref_idle.bmp", "ref_consent.bmp"):
        p = Path(__file__).resolve().parent.parent / "state" / name
        if not p.exists():
            continue
        w, h, bgra = dw.load_bmp(p)
        box = {"x0": 25, "y0": 536, "x1": 526, "y1": 653}
        scan(name, bgra, w, h, box)
    hwnd = dw.find_window()
    if hwnd:
        w, h, bgra = dw.grab(hwnd)
        scan(f"live hwnd={hwnd}", bgra, w, h, {"x0": 25, "y0": 536, "x1": 526, "y1": 653})
    return 0


if __name__ == "__main__":
    sys.exit(main())
