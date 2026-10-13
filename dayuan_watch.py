"""定时给大圆发消息，并自动点掉它弹出的「确认」卡片。

大圆 = 企业微信里的「智能助理」。它的工作方式是：你发一条消息，它就去读未读、
判断该回什么，然后在真正动手发消息前弹一张卡片让你点「确认」/「取消」。

所以要让它无人值守地跑，必须同时做两件事：
  1. 定时往输入框注入一条提示词并发送（默认读 drive.txt）；
  2. 盯着窗口，一旦出现「确认」按钮就点掉。

本文件只依赖 Python 标准库（ctypes + PIL 都不需要）。

为什么不用 UIA：实测「智能助理」整窗只暴露 1 个元素（内容全是自绘），拿不到按钮。
为什么不用 OCR：按钮上的白字 Windows OCR 读不出来（整行丢失）。所以走像素。

判据（2026-10-13 实测取样）：
  「确认」  #267EF0 实心圆角块 52x26，块内有白字（约 3% 像素），右侧紧挨浅灰「取消」
  发送键忙  浅蓝 #DCE9FC 底 + 中心 13x13 小实心蓝（无白字、面积仅 ~140）——靠面积排除

用法：
    python dayuan_watch.py watch                 # 常驻：盯着并定时注入
    python dayuan_watch.py watch --dry-run       # 只看不动手，打印它会做什么
    python dayuan_watch.py watch --no-inject     # 只自动点确认，不注入提示词
    python dayuan_watch.py once                  # 注入一次
    python dayuan_watch.py check IMAGE.bmp       # 对一张图跑判定（自检用）
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DRIVE_TEXT = ROOT / "drive.txt"
AUTO_REPLY_TEXT = ROOT / "auto-reply.txt"
STATE = ROOT / "state"
LOG = STATE / "watch.log"
SHOTS = STATE / "shots"

TITLE = "智能助理"

# ---------------------------------------------------------------- Win32 ----
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

VK_CONTROL = 0x11
VK_A = 0x41
VK_DELETE = 0x2E
VK_MENU = 0x12
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ULONG_PTR = ctypes.c_size_t


class RECT(ctypes.Structure):
    _fields_ = [
        ("L", ctypes.c_long), ("T", ctypes.c_long),
        ("R", ctypes.c_long), ("B", ctypes.c_long),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

gdi32 = ctypes.windll.gdi32
advapi32 = ctypes.windll.advapi32


def _declare_prototypes() -> None:
    """显式声明原型。

    ctypes 默认把返回值当 32 位 int，64 位下句柄（HANDLE/HDC/HBITMAP）会被截断。
    这曾经让"读自己的完整性级别"都失败。全部写清楚，别靠默认。
    """
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int


_declare_prototypes()


def log(line: str) -> None:
    """写日志。stdout 不可用时（无控制台启动、被重定向到已关闭的管道）也不能崩。"""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    text = f"{time.strftime('%m-%d %H:%M:%S')} {line}"
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------ 完整性级别 ----
_TOKEN_QUERY = 0x0008
_TokenIntegrityLevel = 25


def integrity_level(pid: int | None = None) -> str:
    """读进程令牌的完整性级别。

    为什么重要：企业微信跑在 Medium，而受限 shell（沙箱/低完整性）是 Low。
    Low 进程**永远**无法把 Medium 窗口顶到前台，于是 SendInput 打不进字、也点不动按钮。
    这个函数就是用来一眼看出"我为什么点不动"。
    """
    if pid is None:
        handle = kernel32.GetCurrentProcess()
        close = False
    else:
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        close = True
    if not handle:
        return f"打不开进程(拒绝访问) pid={pid}"
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            return "读不到令牌(目标完整性更高)"
        buf = ctypes.create_string_buffer(256)
        ret = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token, _TokenIntegrityLevel, buf, 256, ctypes.byref(ret)
        ):
            return "GetTokenInformation 失败"
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
        rid_ptr = ctypes.cast(sid + 8 + (count - 1) * 4, ctypes.POINTER(ctypes.c_uint32))
        rid = rid_ptr[0]
        name = ("System" if rid >= 0x4000 else "High(管理员)" if rid >= 0x3000
                else "Medium(普通)" if rid >= 0x2000 else "Low(受限)")
        return f"{name} rid=0x{rid:X}"
    finally:
        if token:
            kernel32.CloseHandle(token)
        if close:
            kernel32.CloseHandle(handle)


# ------------------------------------------------------------- 窗口/抓图 ----
def find_window(title: str = TITLE) -> int | None:
    """标题严格等于 title 的可见顶层窗口，取面积最大的那个。"""
    found: list[tuple[int, int]] = []

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value != title:
            return True
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.R - rect.L, rect.B - rect.T
        if w < 200 or h < 200:
            return True
        found.append((w * h, hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][1]


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.L, rect.T, rect.R - rect.L, rect.B - rect.T


def grab(hwnd: int) -> tuple[int, int, bytes]:
    """BitBlt 抓窗口，返回 (w, h, BGRA 字节)。PrintWindow 对这个自绘窗口无效。"""
    _, _, width, height = window_rect(hwnd)
    if width <= 0 or height <= 0 or width > 8000 or height > 8000:
        raise RuntimeError(f"窗口尺寸异常 {width}x{height}（窗口可能被最小化）")
    hdc_w = user32.GetWindowDC(hwnd)
    if not hdc_w:
        raise RuntimeError("GetWindowDC 失败")
    hdc_m = ctypes.windll.gdi32.CreateCompatibleDC(hdc_w)
    hbmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc_w, width, height)
    ctypes.windll.gdi32.SelectObject(hdc_m, hbmp)
    try:
        if not ctypes.windll.gdi32.BitBlt(hdc_m, 0, 0, width, height, hdc_w, 0, 0, SRCCOPY):
            raise RuntimeError("BitBlt 失败")
        info = BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height
        info.biPlanes = 1
        info.biBitCount = 32
        buf = ctypes.create_string_buffer(width * height * 4)
        if not ctypes.windll.gdi32.GetDIBits(
            hdc_m, hbmp, 0, height, buf, ctypes.byref(info), DIB_RGB_COLORS
        ):
            raise RuntimeError("GetDIBits 失败")
        return width, height, buf.raw
    finally:
        ctypes.windll.gdi32.DeleteObject(hbmp)
        ctypes.windll.gdi32.DeleteDC(hdc_m)
        user32.ReleaseDC(hwnd, hdc_w)


def save_bmp(path: Path, width: int, height: int, bgra: bytes) -> None:
    """把 BGRA 缓冲写成 32 位 BMP（自下而上）。"""
    import struct

    row = width * 4
    flipped = b"".join(bgra[y * row:(y + 1) * row] for y in range(height - 1, -1, -1))
    header = bytearray(54)
    header[0:2] = b"BM"
    struct.pack_into("<I", header, 2, 54 + len(flipped))
    struct.pack_into("<I", header, 10, 54)
    struct.pack_into("<I", header, 14, 40)
    struct.pack_into("<i", header, 18, width)
    struct.pack_into("<i", header, 22, height)
    struct.pack_into("<H", header, 26, 1)
    struct.pack_into("<H", header, 28, 32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + flipped)


def load_bmp(path: Path) -> tuple[int, int, bytes]:
    """读 32 位 BMP 成 BGRA 缓冲（自检用；只认本文件 save_bmp 写出的格式）。"""
    data = path.read_bytes()
    if data[:2] != b"BM":
        raise RuntimeError("不是 BMP")
    offset = int.from_bytes(data[10:14], "little")
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    bits = int.from_bytes(data[28:30], "little")
    if bits != 32:
        raise RuntimeError(f"只支持 32 位 BMP，这张是 {bits} 位（请用 save_bmp 生成基准图）")
    flipped = height > 0
    height = abs(height)
    payload = data[offset:]
    row = width * 4
    if len(payload) < row * height:
        raise RuntimeError(f"BMP 数据不足：需要 {row * height} 字节，只有 {len(payload)}")
    rows = [payload[y * row:(y + 1) * row] for y in range(height)]
    if flipped:
        rows.reverse()
    return width, height, b"".join(rows)


# ------------------------------------------------------------- 视觉判定 ----
PRIMARY = (38, 126, 240)      # BGR 顺序见下：我们存的是 BGRA
PRIMARY_TOL = 32
WHITE_MIN = 235


def _px(bgra: bytes, w: int, x: int, y: int) -> tuple[int, int, int]:
    i = (y * w + x) * 4
    return bgra[i + 2], bgra[i + 1], bgra[i]      # -> (R, G, B)


def _is_primary(bgra: bytes, w: int, x: int, y: int) -> bool:
    r, g, b = _px(bgra, w, x, y)
    return (
        abs(r - PRIMARY[0]) <= PRIMARY_TOL
        and abs(g - PRIMARY[1]) <= PRIMARY_TOL
        and abs(b - PRIMARY[2]) <= PRIMARY_TOL
    )


def primary_blobs(bgra: bytes, w: int, h: int, min_area: int = 60,
                  region: tuple[int, int, int, int] | None = None) -> list[dict]:
    """扫主蓝连通块。region=(x0,y0,x1,y1) 限定扫描范围以省时间。"""
    x0, y0, x1, y1 = region if region else (0, 0, w, h)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    rw, rh = x1 - x0, y1 - y0
    if rw <= 0 or rh <= 0:
        return []
    mask = bytearray(rw * rh)
    for y in range(y0, y1):
        base = (y - y0) * rw
        for x in range(x0, x1):
            if _is_primary(bgra, w, x, y):
                mask[base + (x - x0)] = 1
    seen = bytearray(rw * rh)
    out: list[dict] = []
    for yy in range(rh):
        for xx in range(rw):
            i = yy * rw + xx
            if not mask[i] or seen[i]:
                continue
            stack = [(xx, yy)]
            seen[i] = 1
            minx = maxx = xx
            miny = maxy = yy
            count = 0
            while stack:
                cx, cy = stack.pop()
                count += 1
                if cx < minx: minx = cx
                if cx > maxx: maxx = cx
                if cy < miny: miny = cy
                if cy > maxy: maxy = cy
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < rw and 0 <= ny < rh:
                        j = ny * rw + nx
                        if mask[j] and not seen[j]:
                            seen[j] = 1
                            stack.append((nx, ny))
            if count < min_area:
                continue
            bx, by = minx + x0, miny + y0
            bw, bh = maxx - minx + 1, maxy - miny + 1
            white = 0
            for yy2 in range(by, by + bh):
                for xx2 in range(bx, bx + bw):
                    r, g, b = _px(bgra, w, xx2, yy2)
                    if r >= WHITE_MIN and g >= WHITE_MIN and b >= WHITE_MIN:
                        white += 1
            out.append({
                "x": bx, "y": by, "w": bw, "h": bh, "area": count,
                "fill": round(count / float(bw * bh), 3),
                "white": round(white / float(bw * bh), 3),
                "cx": bx + bw // 2, "cy": by + bh // 2,
            })
    out.sort(key=lambda b: -b["area"])
    return out


def _gray_ratio(bgra: bytes, w: int, h: int, x0: int, y0: int, bw: int, bh: int) -> float:
    """浅灰像素占比——「取消」按钮底色 #F2F3F5，卡片底是纯白。"""
    if bw <= 0 or bh <= 0:
        return 0.0
    hit = total = 0
    for y in range(y0, min(h, y0 + bh)):
        for x in range(x0, min(w, x0 + bw)):
            if x < 0:
                continue
            r, g, b = _px(bgra, w, x, y)
            total += 1
            if 232 <= r <= 250 and 232 <= g <= 250 and 232 <= b <= 250 and abs(r - b) <= 8:
                hit += 1
    return hit / float(total) if total else 0.0


def find_consent(bgra: bytes, w: int, h: int,
                 bottom: int | None = None) -> tuple[dict | None, list[dict]]:
    """找「确认」按钮：主蓝实心块 + 块内有白字 + 位于消息区（输入框之上）。

    bottom 由 landmarks() 给出，锚定在检测到的输入框上；不传则退回按比例。
    """
    cut = bottom if bottom is not None else int(h * 0.86)
    blobs = primary_blobs(bgra, w, h, min_area=60, region=(0, 0, w, cut))
    diag: list[dict] = []
    best: dict | None = None
    for b in blobs:
        ok = (
            450 <= b["area"] <= 6000
            and 28 <= b["w"] <= 96
            and 18 <= b["h"] <= 42
            and b["fill"] >= 0.70
            and 0.008 <= b["white"] <= 0.40
            and b["cy"] <= cut
        )
        item = dict(b)
        item["consent"] = ok
        if ok:
            item["partner_gray"] = round(
                _gray_ratio(bgra, w, h, b["x"] + b["w"] + 4, b["y"], 96, b["h"]), 3
            )
            if best is None or b["area"] > best["area"]:
                best = b
        diag.append(item)
    return best, diag


def is_busy(bgra: bytes, w: int, h: int,
            send_xy: tuple[int, int] | None = None) -> tuple[bool, list[dict]]:
    """忙=发送键呈实心蓝小方块（浅蓝底 + 中心小蓝块）。

    send_xy 由 landmarks() 给出；不传则退回"右下角一片区域"的旧做法。
    """
    if send_xy:
        x0, y0 = max(0, send_xy[0] - 40), max(0, send_xy[1] - 40)
        x1, y1 = send_xy[0] + 40, send_xy[1] + 40
    else:
        x0, y0, x1, y1 = int(w * 0.78), int(h * 0.84), w, h
    hits = [
        b for b in primary_blobs(bgra, w, h, min_area=40, region=(x0, y0, x1, y1))
        if 80 <= b["area"] <= 420 and b["w"] <= 32 and b["h"] <= 32
    ]
    return bool(hits), hits


def message_digest(bgra: bytes, w: int, h: int, box: dict | None = None) -> str:
    """消息区指纹（排除底部输入区，避开光标闪动）。"""
    top = int(h * 0.05)
    bottom = (box["y0"] - 8) if box else int(h * 0.80)
    return hashlib.sha1(bgra[top * w * 4:bottom * w * 4]).hexdigest()


# ------------------------------------------------------------------ 地标 ----
# 过去这里写死了一堆比例（输入框 0.45/0.86、发送键 0.911/0.924、消息区 0.5/0.35、
# OCR 裁剪 0.08~0.78）。窗口一挪一动、或者企微改了布局，就全废。
# 改成：先在窗口底部认出"那块白色圆角容器"（装着"添加/当前屏幕"一排 + 输入框），
# 其余位置全部由它推导。实测 552x680 下稳定为 (25,536)-(526,653)，多次采样一致。
WHITE_MIN = 248           # 输入框是纯白；消息区背景是 #F5F7FA(≈246)，低于此值
BOX_MIN_RUN = 0.60        # 一行里白色连续段要占窗口宽度的多少才算输入框行
SEND_INSET_X = 23         # 发送键中心相对容器右下角的内缩（实测 23/24）
SEND_INSET_Y = 24
INPUT_FX, INPUT_FY = 0.35, 0.55   # 输入框点击点：容器内的相对位置（落在提示文字那行）
SCAN_TOP_MIN = 60         # 消息区从标题栏下面开始


def _is_white(p) -> bool:
    return p[0] >= WHITE_MIN and p[1] >= WHITE_MIN and p[2] >= WHITE_MIN


def _longest_white_run(bgra: bytes, w: int, y: int) -> tuple[int, int, int]:
    """某一行里最长的连续白色段，返回 (长度, 起点x, 终点x)。"""
    best = (0, 0, 0)
    start = None
    for x in range(w):
        if _is_white(_px(bgra, w, x, y)):
            if start is None:
                start = x
        elif start is not None:
            if x - start > best[0]:
                best = (x - start, start, x - 1)
            start = None
    if start is not None and w - start > best[0]:
        best = (w - start, start, w - 1)
    return best


def find_input_box(bgra: bytes, w: int, h: int) -> dict | None:
    """找底部那块白色容器。找不到返回 None（调用方退回按比例估算）。"""
    rows: list[tuple[int, int, int, int]] = []
    for y in range(int(h * 0.70), h):
        n, x0, x1 = _longest_white_run(bgra, w, y)
        if n >= BOX_MIN_RUN * w:
            rows.append((y, n, x0, x1))
    if not rows:
        return None
    # 取最长的连续行块（输入框是一整块，中间不会被别的颜色断开）
    best = cur = [rows[0]]
    for r in rows[1:]:
        if r[0] == cur[-1][0] + 1:
            cur.append(r)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [r]
    if len(cur) > len(best):
        best = cur
    if len(best) < 40:          # 太薄，不像输入框
        return None
    x0 = min(r[2] for r in best)
    x1 = max(r[3] for r in best)
    return {"x0": x0, "y0": best[0][0], "x1": x1, "y1": best[-1][0],
            "w": x1 - x0 + 1, "h": best[-1][0] - best[0][0] + 1}


def landmarks(bgra: bytes, w: int, h: int) -> dict:
    """把"该点哪儿"算出来。全部锚定在检测到的输入框上，不写死比例。"""
    box = find_input_box(bgra, w, h)
    if box:
        return {
            "source": "detected",
            "box": box,
            "input": (box["x0"] + int(box["w"] * INPUT_FX),
                      box["y0"] + int(box["h"] * INPUT_FY)),
            "send": (box["x1"] - SEND_INSET_X, box["y1"] - SEND_INSET_Y),
            "message": (w // 2, max(SCAN_TOP_MIN + 20, box["y0"] // 2)),
            "scan_top": SCAN_TOP_MIN,
            "scan_bottom": max(SCAN_TOP_MIN + 40, box["y0"] - 8),
        }
    # 兜底：旧的比例法。日志会提醒，doctor 也会显示 source=fallback
    return {
        "source": "fallback",
        "box": None,
        "input": (int(w * INPUT_X), int(h * INPUT_Y)),
        "send": (int(w * SEND_X), int(h * SEND_Y)),
        "message": (int(w * 0.5), int(h * 0.35)),
        "scan_top": int(h * 0.08),
        "scan_bottom": int(h * 0.78),
    }


# ---------------------------------------------------------------- 输入 ----
def set_clipboard(text: str) -> None:
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    data = text.encode("utf-16le") + b"\x00\x00"
    opened = False
    for _ in range(20):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)
    if not opened:
        raise RuntimeError("打不开剪贴板")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("分配剪贴板内存失败")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            raise RuntimeError("锁定剪贴板内存失败")
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()


def _send(inputs: list[INPUT]) -> None:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise RuntimeError(f"SendInput 只送出 {sent}/{len(inputs)}")


user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def force_foreground(hwnd: int) -> bool:
    """把窗口顶到前台。

    单独 SetForegroundWindow 会被前台锁拒绝（用户正在别的窗口打字时必失败）。
    AttachThreadInput 把自己的输入队列挂到当前前台线程上，就能绕过这个锁——
    cua-driver 的 bring_to_front 用的也是这一招。
    """
    user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    me = kernel32.GetCurrentThreadId()
    threads = []
    for target in (user32.GetWindowThreadProcessId(fg, None),
                   user32.GetWindowThreadProcessId(hwnd, None)):
        if target and target != me and target not in threads:
            if user32.AttachThreadInput(me, target, True):
                threads.append(target)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        for target in threads:
            user32.AttachThreadInput(me, target, False)
    time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


def focus(hwnd: int) -> bool:
    if force_foreground(hwnd):
        return True
    # 最后的土办法：敲一下 Alt 解除前台锁，再试一次
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return bool(user32.SetForegroundWindow(hwnd))


def click_at(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.08)
    _send([
        INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)),
        INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)),
    ])


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [POINT]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


def post_click(hwnd: int, screen_x: int, screen_y: int) -> int:
    """把点击 PostMessage 给该坐标下最深的窗口。

    这是实测**唯一**能点动「确认」的办法：SendInput（抢焦点 + 合成鼠标）
    在这个 Qt 自绘窗口上毫无反应，而 PostMessage 一击即中。
    额外好处：全程不抢前台焦点。
    """
    target = user32.WindowFromPoint(POINT(screen_x, screen_y)) or hwnd
    pt = POINT(screen_x, screen_y)
    user32.ScreenToClient(target, ctypes.byref(pt))
    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.06)
    user32.PostMessageW(target, WM_LBUTTONUP, 0, lp)
    return target


def tap(vk: int) -> None:
    _send([
        INPUT(type=1, ki=KEYBDINPUT(vk, 0, 0, 0, 0)),
        INPUT(type=1, ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)),
    ])


def chord(modifier: int, vk: int) -> None:
    _send([
        INPUT(type=1, ki=KEYBDINPUT(modifier, 0, 0, 0, 0)),
        INPUT(type=1, ki=KEYBDINPUT(vk, 0, 0, 0, 0)),
        INPUT(type=1, ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)),
        INPUT(type=1, ki=KEYBDINPUT(modifier, 0, KEYEVENTF_KEYUP, 0, 0)),
    ])


def type_unicode(text: str) -> None:
    inputs: list[INPUT] = []
    for ch in text:
        code = ord(ch)
        inputs.append(INPUT(type=1, ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        inputs.append(INPUT(type=1, ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
    _send(inputs)


def point(hwnd: int, x_ratio: float, y_ratio: float) -> tuple[int, int]:
    left, top, width, height = window_rect(hwnd)
    return left + int(width * x_ratio), top + int(height * y_ratio)


# 仅在 landmarks() 认不出输入框时用作兜底（沿用 driver.py 的实测值）
INPUT_X, INPUT_Y = 0.45, 0.86
SEND_X, SEND_Y = 0.911, 0.924


def double_ctrl(gap: float = 0.07) -> None:
    """双击 Ctrl —— 企微「唤醒智能助理」的全局热键（实测有效）。

    比点主窗口左栏靠得住：那个入口 UIA 拿不到元素（整窗 0 个 descendant），
    只能坐标点；而这个热键不依赖任何窗口位置，窗口关着也能把它叫出来。
    """
    tap(VK_CONTROL)
    time.sleep(gap)
    tap(VK_CONTROL)


def wake(timeout: float = 12.0, quiet: bool = False) -> int | None:
    """确保「智能助理」窗口存在且没被最小化，返回 hwnd。

    注意窗口句柄每次重开都会变（实测 3279190 -> 3737942），所以永不缓存。
    """
    hwnd = find_window()
    if hwnd is not None:
        left, _, _, _ = window_rect(hwnd)
        if left >= -10000:
            return hwnd
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.6)
        left, _, _, _ = window_rect(hwnd)
        if left >= -10000:
            return hwnd

    if not quiet:
        log("窗口不在，双击 Ctrl 唤醒")
    double_ctrl()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        hwnd = find_window()
        if hwnd is None:
            continue
        left, _, _, _ = window_rect(hwnd)
        if left < -10000:
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.5)
            left, _, _, _ = window_rect(hwnd)
        if left >= -10000:
            if not quiet:
                log(f"已唤醒 hwnd={hwnd}")
            return hwnd
    if not quiet:
        log("唤醒失败（双击 Ctrl 没把窗口叫出来）")
    return None


def ensure_foreground(hwnd: int) -> None:
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("焦点不在「智能助理」，停手，避免把字打进别的窗口")


def load_prompt(path: Path | None = None) -> str:
    p = path or DRIVE_TEXT
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"{p.name} 是空的")
    return text


# ---------------------------------------------------------------- 读对话 ----
# 「智能助理」窗口的复制上限：纯文本 / CF_HTML / WeChat_RichEdit_Format 三种格式
# 都只给约 128 字符的预览（实测），全文只在 8000 字节的 "WeWork Message" 富格式里。
# 所以读对话 = 剪贴板拿精确尾部 + OCR 拿全量，两者互补。
CF_UNICODETEXT = 13
kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.restype = wintypes.BOOL


def read_clipboard() -> str:
    if not user32.OpenClipboard(None):
        raise RuntimeError("打不开剪贴板")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            raw = ctypes.string_at(ptr, kernel32.GlobalSize(handle))
            return raw.decode("utf-16le", "replace").rstrip("\x00")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


TRANSCRIPT_HEAD = re.compile(r"^(?P<who>.{1,40}?)\s+(?P<when>\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*$")


def parse_transcript(text: str) -> list[dict]:
    """把复制出来的对话切成 [{who, when, text}]。

    实测格式（注意是 CRLF，且两轮之间空行数量不固定，所以逐行扫描最稳）：
        大圆 10/13 11:00:09
        已成功给……发送了"你好"。
        <空行>
        市场部-李四 10/13 11:05:43
        给文件传输助手发一条消息：……
    """
    out: list[dict] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        m = TRANSCRIPT_HEAD.match(line.strip())
        if m:
            out.append({"who": m.group("who").strip(), "when": m.group("when"), "text": ""})
            continue
        if not out:
            if line.strip():
                out.append({"who": "", "when": "", "text": line.strip()})
            continue
        if line.strip():
            out[-1]["text"] = (out[-1]["text"] + "\n" + line).strip() if out[-1]["text"] else line.strip()
    return [m for m in out if m["who"] or m["text"]]


def copy_all_text(hwnd: int, marks: dict | None = None) -> str:
    """全选 + 复制窗口里的对话，返回剪贴板文本。需要真实前台焦点。"""
    before = read_clipboard()
    if marks is None:
        w0, h0, shot = grab(hwnd)
        marks = landmarks(shot, w0, h0)
    focus(hwnd)
    time.sleep(0.3)
    ensure_foreground(hwnd)
    # 先点一下消息区，避免焦点停在输入框里
    left, top, _, _ = window_rect(hwnd)
    mx, my = marks["message"]
    click_at(left + mx, top + my)
    time.sleep(0.2)
    force_foreground(hwnd)
    time.sleep(0.15)
    chord(VK_CONTROL, VK_A)
    time.sleep(0.15)
    chord(VK_CONTROL, VK_C)
    time.sleep(0.45)
    after = read_clipboard()
    if after == before and not after:
        raise RuntimeError("复制后剪贴板仍为空")
    return after


VK_C = 0x43

# 剪贴板里那份 8000 字节的 "WeWork Message"（Boost 序列化）装着**全文**；
# 纯文本 / CF_HTML / WeChat_RichEdit_Format 都只有 ~128 字符预览（实测）。
WEWORK_FORMAT = "WeWork Message"
_UTF8_RUN = re.compile(rb"(?:[\x20-\x7e]|[\xc2-\xf4][\x80-\xbf]+){4,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 中文 + 中文标点 + 全角符号（。、《》等都在 \u3000-\u303f / \uff00-\uffef）
_CJKP = r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]"
_PREFIX_JUNK = re.compile(r"^[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]{1,3}(?=" + _CJKP + ")")
_SUFFIX_JUNK = re.compile(r"(?<=" + _CJKP + r")[^a-zA-Z0-9\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]{1,3}$")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 这些是界面标签，不是消息内容
_WEWORK_NOISE = ("智能助理", "发送消息", "发送消息完成", "发送中", "已完成", "正在执行")


def read_clipboard_format(name: str) -> bytes:
    fid = user32.RegisterClipboardFormatW(name)
    if not fid or not user32.OpenClipboard(None):
        return b""
    try:
        handle = user32.GetClipboardData(fid)
        if not handle:
            return b""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return b""
        try:
            return ctypes.string_at(ptr, kernel32.GlobalSize(handle))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _wework_runs(data: bytes) -> list[str]:
    """从 Boost 序列化流里按顺序捞出可读中文片段，去掉相邻重复。"""
    out: list[str] = []
    for m in _UTF8_RUN.finditer(data):
        try:
            s = m.group().decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(_CJK.findall(s)) >= 2 and (not out or out[-1] != s):
            out.append(s)
    return out


def _clean_md(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = text.replace("**", "").replace("\u200b", "")
    text = text.strip()
    # Boost 序列化把长度前缀/结构字节黏在正文头尾（实测头部一个 'H'、尾部 ':\\'）。
    # 只在紧邻中文处、且长度 <=3、且不含空格时才当垃圾剥掉，避免误伤 "CLI 联通测试"。
    text = _PREFIX_JUNK.sub("", text)
    text = _SUFFIX_JUNK.sub("", text)
    return text.strip()


def newest_reply(debug: bool = False) -> str | None:
    """取大圆最新说的一句（读剪贴板富格式）。

    警告：这是**不可靠**的兜底。剪贴板三份文本都被"从对话开头"截断
    （纯文本 128 字符、CF_HTML ~121 字符、WeWork Message 8000 字节），
    对话一长，最新回复就落在截断之外。主力是 `ocr_lines()`。
    """
    runs = _wework_runs(read_clipboard_format(WEWORK_FORMAT))
    if debug:
        for i, s in enumerate(runs):
            print(f"  run[{i}] {s[:120]!r}")
    for s in reversed(runs):
        c = _clean_md(s)
        if not c or len(c) < 4:
            continue
        if c in _WEWORK_NOISE or any(c.startswith(n) for n in _WEWORK_NOISE):
            continue
        if "智能助理的对话" in c or "http" in c.lower():
            continue
        return c
    return None


# ------------------------------------------------------------------- OCR ----
# 截图永远显示最新内容，所以"读最新"用 OCR；2x 放大后 Windows OCR 精度足够
# （实测 1x 会把"联通测试"识成乱码，2x 一字不差）。
OCR_HELPER = ROOT / "ocr_image.ps1"
_OCR_PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
    "System32/WindowsPowerShell/v1.0/powershell.exe")


def crop_rows(bgra: bytes, w: int, y0: int, y1: int) -> tuple[int, int, bytes]:
    """裁出 y0..y1 这几行。放大交给 OCR 助手（.NET HighQualityBicubic 比手写好）。"""
    y0 = max(0, y0)
    rows = []
    for y in range(y0, y1):
        row = bgra[y * w * 4:(y + 1) * w * 4]
        if len(row) < w * 4:
            break
        rows.append(row)
    return w, len(rows), b"".join(rows)


def run_ocr(path: Path, scale: float = 3.0) -> list[dict]:
    """调 ocr_image.ps1（.NET 放大 + Windows OCR），返回 [{text,x,y,w,h}]（坐标已还原到 1x）。"""
    import json
    import subprocess

    proc = subprocess.run(
        [str(_OCR_PS), "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(OCR_HELPER), "-Path", str(path), "-Scale", str(scale)],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:300])
    payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    out: list[dict] = []
    for line in payload.get("lines", []):
        text = " ".join(line["text"].split())
        if not text:
            continue
        out.append({
            "text": text,
            "x": int(line["x"] / scale),
            "y": int(line["y"] / scale),
            "w": int(line["w"] / scale),
            "h": max(8, int(line["h"] / scale)),
        })
    return out


def ocr_lines(hwnd: int, scale: float = 3.0,
              marks: dict | None = None) -> list[dict]:
    """截图 -> 裁消息区 -> OCR。

    裁剪范围锚定在检测到的输入框上（scan_top..scan_bottom），窗口缩放也跟着走。
    y 很关键：长句会被 OCR 拆成多行，靠 y 才能判断哪些行属于同一个气泡。
    """
    w, h, bgra = grab(hwnd)
    if marks is None:
        marks = landmarks(bgra, w, h)
    cw, ch, cropped = crop_rows(bgra, w, marks["scan_top"], marks["scan_bottom"])
    tmp = STATE / "ocr_tmp.bmp"
    save_bmp(tmp, cw, ch, cropped)
    return run_ocr(tmp, scale)


# ------------------------------------------------------------ 确认卡内容 ----
# 光按"形状"批准是盲点：任何蓝底白字主按钮 + 右侧浅灰取消键都会被点。
# 这里把卡片上的「发送消息给: X / 消息内容: Y」OCR 出来，让 CLI 知道自己要批什么，
# 也才有条件做白名单/黑名单。
# 实测单倍数不够：2x 认对内容(你好)但收件人认错一个字，3x 认对收件人但内容花掉。
# 所以两轮合起来用。
CARD_OCR_SCALES = (2.0, 3.0)
CANCEL_DX = 55          # 「取消」中心相对「确认」中心的水平偏移（实测 410 -> 465）
_LEAD_JUNK = re.compile(r"^[\s：:．.·,，、;；\-—|]+")


def _after_keyword(text: str, keyword: str) -> str | None:
    """在 OCR 行里找 keyword 并返回它后面的内容。

    坑：OCR 会在字间塞空格，而且**冒号有时整个丢掉**（实测 3x 那轮就是
    "发 送 消 息 给 研 发 部 · 张 三"，没有分隔符）。
    所以关键词按"字符之间可有空格"来匹配，分隔符只当可选的前导杂字剥掉。
    """
    pattern = r"\s*".join(re.escape(c) for c in keyword)
    m = re.search(pattern, text)
    if not m:
        # 常见误识：消->氵肖 / 内->亻子
        alt = keyword.replace("消", "氵肖").replace("内", "亻子")
        pattern = r"\s*".join(re.escape(c) for c in alt)
        m = re.search(pattern, text)
        if not m:
            return None
    rest = _LEAD_JUNK.sub("", text[m.end():]).strip()
    return rest or None


def _card_region(btn: dict, w: int, h: int) -> tuple[int, int, int, int]:
    """确认按钮所在的卡片区域（按钮那一行 + 下面内容行）。"""
    y0 = max(0, btn["y"] - 22)
    y1 = min(h, btn["y"] + 68)
    return 28, y0, w - 28, y1


def read_card(hwnd: int, btn: dict) -> dict:
    """OCR 出确认卡上的收件人与内容。失败时返回空字段，不抛异常（不能因为读不到就不批）。"""
    try:
        w, h, bgra = grab(hwnd)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "recipient": "", "content": "", "lines": []}
    x0, y0, x1, y1 = _card_region(btn, w, h)
    cw, ch, cropped = crop_rows(bgra, w, y0, y1)
    lines: list[str] = []
    for scale in CARD_OCR_SCALES:
        try:
            tmp = STATE / f"card_{int(scale)}x.bmp"
            save_bmp(tmp, cw, ch, cropped)
            for ln in run_ocr(tmp, scale):
                t = _tighten(ln["text"]).strip()
                if t and t not in lines:
                    lines.append(t)
        except Exception:  # noqa: BLE001
            continue

    recipient = content = ""
    for t in lines:
        r = _after_keyword(t, "发送消息给") or _after_keyword(t, "发送给")
        if r and len(r) > len(recipient):
            recipient = r
            continue
        c = _after_keyword(t, "消息内容")
        if c and len(c) > len(content):
            content = c
    return {"recipient": recipient, "content": content, "lines": lines}


def cancel_confirm(hwnd: int, btn: dict) -> bool:
    """点「取消」而不是「确认」。用于 --review：知道要发什么，但先不发。"""
    left, top, _, _ = window_rect(hwnd)
    post_click(hwnd, left + btn["cx"] + CANCEL_DX, top + btn["cy"])
    time.sleep(0.9)
    w, h, shot = grab(hwnd)
    still, _ = find_consent(shot, w, h)
    if still:
        log("点「取消」没生效，卡片还在")
        return False
    return True


def _name_hit(pattern: str, name: str) -> bool:
    """名单匹配。

    收件人名字来自 OCR，必然有误识（实测 张三 -> 张二），而且卡片上写的是
    "研发部·张三" 这种带前缀的全称。所以：
      * 直接用整个全称去比相似度会被前缀拉低（实测只有 0.36），必须**滑动窗口比子串**；
      * 阈值放到 0.6 —— 3 个字错 1 个 = 0.667，能容忍单字误识。
    """
    import difflib

    p, n = _squash(pattern), _squash(name)
    if not p or not n:
        return False
    if p in n or n in p:
        return True
    best = 0.0
    for width in range(max(1, len(p) - 1), len(p) + 2):
        for i in range(0, max(1, len(n) - width + 1)):
            chunk = n[i:i + width]
            if not chunk:
                continue
            best = max(best, difflib.SequenceMatcher(None, p, chunk).ratio())
            if best >= 0.6:
                return True
    return False


def card_allowed(card: dict, allow: list[str], deny: list[str]) -> tuple[bool, str]:
    """按收件人名做白/黑名单判断。

    方向很关键：**设了白名单却读不出收件人时必须拦下来**（fail-closed），
    否则 OCR 一失手，白名单就等于不存在。
    """
    name = card.get("recipient", "")
    readable = bool(_squash(name))
    for d in deny:
        if readable and _name_hit(d, name):
            return False, f"命中黑名单「{d}」"
    if allow:
        if not readable:
            return False, "设了白名单但读不出收件人，保守起见不发"
        for a in allow:
            if _name_hit(a, name):
                return True, f"命中白名单「{a}」"
        return False, "不在白名单里"
    if not readable:
        return True, "读不出收件人，且没设白名单，按放行处理"
    return True, "无名单限制"


_OCR_NOISE = ("已完成", "正在执行", "添加", "当前屏幕", "问问大圆", "发送消息",
              "发送中", "取消", "确认", "重新生成", "复制", "已完成任务")


def _squash(text: str) -> str:
    return re.sub(r"[\s\u3000]", "", text)


def _is_noise_line(text: str) -> bool:
    """判断 OCR 出来的一行是不是界面标签 / 碎片。

    实测 OCR 在不同放大倍数下会把"已完成"识成"已成"或"已电成"，
    所以不能只做相等比较——还要看它是不是噪声词的子串。
    """
    sq = _squash(text)
    if len(sq) <= 3:
        return True
    if not _CJK.search(sq) and len(sq) < 8:
        return True
    for n in _OCR_NOISE:
        ns = _squash(n)
        if sq in ns or ns in sq:
            return True
    return False


def _tighten(text: str) -> str:
    """去掉 OCR 塞在中文之间的空格，但保留 "CLI 联通测试" 里那种词间空格。"""
    cjk = "[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]"
    return re.sub(rf"(?<={cjk})[ \t]+(?={cjk})", "", text)


def reply_after_prompt(lines: list[dict], prompt: str) -> str | None:
    """在 OCR 行里按"我发的那句"定位，取它下面那个气泡的正文。

    两个坑：
      * 不能用精确匹配——OCR 会把"一句话"识成"一匀话"；
      * 长句会被拆成多行，锚点只命中第一行，续行会当成回复混进来。
    后者用 y 解决：锚点行下方 1.5 倍行高以内的一律算同一气泡，丢掉。
    """
    import difflib

    target = _squash(prompt)
    best_i, best_ratio = -1, 0.0
    for i, ln in enumerate(lines):
        s = _squash(ln["text"])
        if not s:
            continue
        ratio = difflib.SequenceMatcher(None, s, target).ratio()
        if ratio > best_ratio:
            best_i, best_ratio = i, ratio
    if best_i < 0 or best_ratio < 0.45:
        body = lines
        cutoff = -1.0
    else:
        anchor = lines[best_i]
        cutoff = anchor["y"] + anchor["h"] * 1.5
        body = lines[best_i + 1:]
    kept = [
        _tighten(ln["text"].strip())
        for ln in body
        if ln["y"] > cutoff and not _is_noise_line(ln["text"])
    ]
    return "\n".join(kept) if kept else None


def read_conversation(hwnd: int, tail: bool = True) -> dict:
    """读当前对话：剪贴板纯文本给带发言人/时间的顺序，富格式补上被截掉的最新消息。"""
    raw = copy_all_text(hwnd)
    msgs = parse_transcript(raw)
    if tail and len(msgs) > 8:
        msgs = msgs[-8:]
    result = {"raw": raw, "messages": msgs, "truncated": True}
    try:
        newest = newest_reply()
    except Exception:  # noqa: BLE001
        newest = None
    if newest:
        result["newest"] = newest
        last = msgs[-1]["text"] if msgs else ""
        if newest not in last and not last.startswith(newest[:20]):
            msgs.append({"who": "大圆", "when": "最新", "text": newest})
    return result


def last_from(msgs: list[dict], who: str = "大圆") -> dict | None:
    for m in reversed(msgs):
        if who in m["who"]:
            return m
    return None


# ------------------------------------------------------------------- CLI ----
def cmd_status() -> int:
    import json

    out: dict = {"window": False}
    hwnd = find_window()
    if hwnd is None:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    left, top, w, h = window_rect(hwnd)
    out.update({"window": True, "hwnd": hwnd, "pid": pid.value,
                "rect": [left, top, w, h], "minimized": left < -10000,
                "integrity": integrity_level()})
    if left >= -10000:
        try:
            gw, gh, shot = grab(hwnd)
            marks = landmarks(shot, gw, gh)
            btn, _ = find_consent(shot, gw, gh, marks["scan_bottom"])
            busy, _ = is_busy(shot, gw, gh, marks["send"])
            out.update({"busy": busy, "pending_confirm": btn,
                        "landmarks": marks, "landmark_source": marks["source"]})
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def ocr_best(hwnd: int, scales: tuple[float, ...] = (2.0, 3.0, 4.0)) -> tuple[list[dict], float]:
    """多倍数跑 OCR，挑识别出中文字最多的那轮。

    OCR 对放大倍数很敏感：实测同一张图 3x 会把整行漏掉、2x 一字不差。
    用"识别到的中文字数"当质量分，简单但有效。
    """
    best: tuple[list[dict], float] | None = None
    best_score = -1
    for scale in scales:
        try:
            lines = ocr_lines(hwnd, scale)
        except Exception:  # noqa: BLE001
            continue
        score = sum(len(_CJK.findall(ln["text"])) for ln in lines)
        if score > best_score:
            best, best_score = (lines, scale), score
    if best is None:
        raise RuntimeError("所有放大倍数的 OCR 都失败了")
    return best


def cmd_read(as_json: bool) -> int:
    import json

    hwnd = wake()
    if hwnd is None:
        log("叫不出「智能助理」窗口")
        return 2
    try:
        lines, scale = ocr_best(hwnd)
    except Exception as exc:  # noqa: BLE001
        log(f"OCR 失败，改用剪贴板：{exc}")
        convo = read_conversation(hwnd)
        for m in convo["messages"]:
            print(f"[{m['when']}] {m['who']}: {m['text']}")
        return 0
    if as_json:
        print(json.dumps({"scale": scale, "ocr_lines": lines}, ensure_ascii=False, indent=2))
    else:
        log(f"（OCR 放大 {scale}x）")
        for line in lines:
            print(f"[y={line['y']:>4}] {_tighten(line['text'])}")
    return 0


def wait_until_done(hwnd: int, baseline: str, timeout: float,
                    review: bool = False, allow: list[str] | None = None,
                    deny: list[str] | None = None,
                    on_card=None) -> bool:
    """等大圆干完这一轮。

    判定依据是**对话内容变了、然后又稳定了**，而不是"没在忙"——
    刚发完那一瞬间它当然不忙，拿"空闲"当完成会立刻返回上一次的旧结果（踩过这个坑）。

    期间出现的确认卡一律交给 handle_confirm：它会先读懂"发给谁/发什么"，
    再按 review / 名单策略决定批还是撤。
    """
    deadline = time.time() + timeout
    started = False
    stable = 0
    prev = baseline
    while time.time() < deadline:
        time.sleep(1.2)
        try:
            w, h, shot = grab(hwnd)
        except Exception:  # noqa: BLE001
            continue
        marks = landmarks(shot, w, h)
        btn, _ = find_consent(shot, w, h, marks["scan_bottom"])
        if btn:
            done, card = handle_confirm(hwnd, btn, shot, w, h, review, allow, deny)
            if on_card is not None:
                on_card(card, done)
            started, stable, prev = True, 0, None
            continue
        busy, _ = is_busy(shot, w, h, marks["send"])
        digest = message_digest(shot, w, h, marks["box"])
        if digest != baseline:
            started = True
        if busy:
            stable = 0
        elif digest == prev:
            stable += 1
        else:
            stable = 0
        prev = digest
        if started and stable >= 3:
            return True
    log(f"等待超时（{'内容变过' if started else '内容一直没变'}）")
    return started


def cmd_ask(prompt: str, timeout: float, out_file: str | None, dry_run: bool) -> int:
    """发一条给大圆，等它跑完（含自动确认），返回它最后说的话。

    退出码：0 拿到回复 / 2 窗口问题 / 3 超时且无变化 / 4 没读到回复
    """
    hwnd = wake()                     # 没开/最小化都自己叫起来
    if hwnd is None:
        log("叫不出「智能助理」窗口（企微没运行？）")
        return 2
    if dry_run:
        log(f"[dry-run] 会发给大圆：{prompt}")
        return 0

    bw, bh, baseline = grab(hwnd)
    marks = landmarks(baseline, bw, bh)
    base_digest = message_digest(baseline, bw, bh, marks["box"])

    inject(hwnd, prompt, marks)
    log(f"已发送：{prompt}")

    if not wait_until_done(hwnd, base_digest, timeout):
        return 3

    # 主力：OCR 截图（永远显示最新内容）。剪贴板富格式只作兜底。
    reply = None
    for scale in (3.0, 2.0, 4.0):
        try:
            lines = ocr_lines(hwnd, scale)
        except Exception as exc:  # noqa: BLE001
            log(f"OCR(scale={scale}) 失败：{exc}")
            continue
        if os.environ.get("DAYUAN_DEBUG_OCR"):
            for i, l in enumerate(lines):
                print(f"  ocr[{scale}][{i}] y={l['y']} {l['text']!r}", file=sys.stderr)
        reply = reply_after_prompt(lines, prompt)
        if reply:
            break
    if not reply:
        try:
            reply = newest_reply()
        except Exception:  # noqa: BLE001
            reply = None
    if not reply:
        log("没读到回复")
        return 4
    print(reply)
    if out_file:
        Path(out_file).write_text(reply, encoding="utf-8")
    return 0


# /auto-reply 的默认提示词。设计要点：
#   * 先判"要不要回"，别见谁都回；
#   * 明确禁止重复回复（这是定时注入最容易出的问题）；
#   * 要求口语化、短，别端着；
#   * 群里只在被 @ 到时回；
#   * 结尾给一份清单，方便从日志里看它到底干了什么。
AUTO_REPLY_PROMPT = """自动巡检：处理我的未读消息。

1. 先看有哪些未读会话。
2. 只回**确实需要我本人回应**的：有人问我事情、等我拍板、或者明显在等我回话。
   通知、广告、系统消息、群里的刷屏闲聊，一律跳过。
3. **绝不重复回复**：如果你在这个对话里已经回过，或者最后一条消息本来就是你发的，直接跳过。
4. 回复要短（一两句），像我自己在打字，口语化，别用"作为AI""很高兴为您服务"这种腔调。
5. 群聊只在明确 @ 到我 时才回，且只回一句。
6. 拿不准要不要回的，就不要回。

做完给我一份简报，三行以内：回了谁、回了什么、跳过了谁和为什么。
如果没有任何未读，只回四个字：没有未读。
"""


def ensure_auto_prompt() -> Path:
    """auto-reply.txt 不存在就写一份默认的。用户改这个文件即可调整行为。"""
    if not AUTO_REPLY_TEXT.exists():
        AUTO_REPLY_TEXT.write_text(AUTO_REPLY_PROMPT, encoding="utf-8")
    return AUTO_REPLY_TEXT


def cmd_auto_reply(interval: float, tick: float, review: bool, dry_run: bool,
                   allow: list[str], deny: list[str], once: bool = False,
                   timeout: float = 600) -> int:
    """注入提示词 -> 大圆查未读并拟回复 -> CLI 读懂确认卡再决定批/撤。

    once=True 表示"完整跑一轮就退出"：注入后要等它做完、该批的批、该撤的撤，
    最后把它这一轮的结论读出来。**只注入不管卡片是不行的**——卡片没人点会
    "审批超时，操作未能执行"，等于白跑。
    """
    pf = ensure_auto_prompt()
    prompt = load_prompt(pf)

    if not once:
        try:
            loop(interval, True, True, dry_run, tick, prompt_file=pf,
                 review=review, allow=allow, deny=deny, label="auto-reply")
        except KeyboardInterrupt:
            log("[auto-reply] 手动停止")
        return 0

    # --- 跑一轮 ---
    hwnd = wake()
    if hwnd is None:
        log("叫不出「智能助理」窗口")
        return 2
    if dry_run:
        log(f"[dry-run] 会注入：{prompt}")
        return 0

    bw, bh, baseline = grab(hwnd)
    marks = landmarks(baseline, bw, bh)
    base_digest = message_digest(baseline, bw, bh, marks["box"])

    cards: list[dict] = []

    def on_card(card: dict, done: bool) -> None:
        cards.append({**card, "handled": done})

    inject(hwnd, prompt, marks)
    log(f"[auto-reply] 已注入（{pf.name}）：{prompt.splitlines()[0]}…")

    ok = wait_until_done(hwnd, base_digest, timeout, review, allow, deny, on_card)

    # 把它这一轮的结论读出来
    reply = None
    for scale in (3.0, 2.0, 4.0):
        try:
            lines = ocr_lines(hwnd, scale, marks)
        except Exception:  # noqa: BLE001
            continue
        reply = reply_after_prompt(lines, prompt)
        if reply:
            break
    if not reply:
        try:
            reply = newest_reply()
        except Exception:  # noqa: BLE001
            reply = None

    log(f"[auto-reply] 一轮结束 完成={ok} 处理卡片={len(cards)} 张")
    for c in cards:
        log(f"  卡片：发给「{c.get('recipient') or '?'}」内容「{c.get('content') or '?'}」"
            f"{'已处理' if c.get('handled') else '未处理'}")
    if reply:
        print(reply)
    return 0 if ok else 3


def cmd_wake(dry_run: bool = False) -> int:
    """把「智能助理」窗口叫出来（双击 Ctrl），并报告它的状态。"""
    import json

    before = find_window()
    if dry_run:
        log("[dry-run] 会双击 Ctrl 唤醒")
        return 0
    hwnd = wake()
    if hwnd is None:
        log("唤醒失败")
        return 2
    left, top, w, h = window_rect(hwnd)
    info = {"hwnd": hwnd, "rect": [left, top, w, h], "already_open": before == hwnd}
    try:
        gw, gh, shot = grab(hwnd)
        marks = landmarks(shot, gw, gh)
        info["landmark_source"] = marks["source"]
        info["input_box"] = marks["box"]
        info["input_point"] = marks["input"]
        info["send_point"] = marks["send"]
        info["ocr_range"] = [marks["scan_top"], marks["scan_bottom"]]
        if marks["source"] != "detected":
            log("警告：没认出输入框，会退回按比例估算")
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def inject(hwnd: int, prompt: str, marks: dict | None = None) -> dict:
    """把提示词打进输入框并发送。

    输入框和发送键的位置由 landmarks() 从图里认出来（锚定白色输入容器），
    不再依赖写死的比例，所以窗口挪动/缩放都跟着走；认不出来才退回比例法。
    返回用到的地标，便于日志和排查。
    """
    if marks is None:
        w, h, shot = grab(hwnd)
        marks = landmarks(shot, w, h)
    if marks["source"] != "detected":
        log("警告：没认出输入框，退回按比例估算位置（doctor 可查）")

    focus(hwnd)
    time.sleep(0.35)
    left, top, _, _ = window_rect(hwnd)
    ix, iy = marks["input"]
    click_at(left + ix, top + iy)
    time.sleep(0.25)
    ensure_foreground(hwnd)
    chord(VK_CONTROL, VK_A)
    tap(VK_DELETE)
    time.sleep(0.05)
    type_unicode(prompt)
    time.sleep(0.3)
    ensure_foreground(hwnd)
    sx, sy = marks["send"]
    click_at(left + sx, top + sy)
    return marks


KEEP_SHOTS = 50


def prune_shots(keep: int = KEEP_SHOTS) -> None:
    """审计截图只留最近 keep 张。每张 BMP 约 1.5MB，无人值守跑久了会撑爆磁盘。"""
    try:
        files = sorted(SHOTS.glob("approve-*.bmp"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def handle_confirm(hwnd: int, btn: dict, shot: bytes, w: int, h: int,
                   review: bool = False, allow: list[str] | None = None,
                   deny: list[str] | None = None) -> tuple[bool, dict]:
    """处理一张确认卡：先读懂它，再决定批还是撤。

    返回值 (卡片是否已处理掉, 卡片信息)。先读后批，是为了不再"盲点批准"——
    日志里会留下"将要发给谁、发什么"，也才有条件做名单过滤。
    """
    card = read_card(hwnd, btn)
    who = card.get("recipient") or "?"
    what = card.get("content") or "?"
    log(f"确认卡：发给「{who}」内容「{what}」")

    ok, why = card_allowed(card, allow or [], deny or [])
    if review:
        log(f"--review：只审不发，点「取消」（{why}）")
        return cancel_confirm(hwnd, btn), card
    if not ok:
        log(f"策略拦截：{why} → 点「取消」")
        return cancel_confirm(hwnd, btn), card
    return approve(hwnd, btn, shot, w, h), card


def approve(hwnd: int, btn: dict, shot: bytes, w: int, h: int) -> bool:
    """点「确认」。先用 PostMessage（不抢焦点），点不动再回退到抢焦点的真实鼠标。

    返回 True 表示卡片确实消失了。
    """
    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d-%H%M%S")
    save_bmp(SHOTS / f"approve-{stamp}.bmp", w, h, shot)
    prune_shots()
    left, top, _, _ = window_rect(hwnd)
    sx, sy = left + btn["cx"], top + btn["cy"]

    target = post_click(hwnd, sx, sy)
    time.sleep(0.9)
    w2, h2, shot2 = grab(hwnd)
    still, _ = find_consent(shot2, w2, h2)
    if not still:
        log(f"已点「确认」 center=({btn['cx']},{btn['cy']}) area={btn['area']} "
            f"white={btn['white']} 方式=PostMessage 截图=shots/approve-{stamp}.bmp")
        return True

    log(f"PostMessage 点击未生效（target={target}），回退到真实鼠标事件")
    save_bmp(SHOTS / f"approve-{stamp}-retry.bmp", w2, h2, shot2)
    focus(hwnd)
    time.sleep(0.3)
    ensure_foreground(hwnd)
    click_at(sx, sy)
    time.sleep(0.9)
    w3, h3, shot3 = grab(hwnd)
    still2, _ = find_consent(shot3, w3, h3)
    if not still2:
        log(f"已点「确认」 center=({btn['cx']},{btn['cy']}) 方式=SendInput")
        return True
    log(f"两种点击都没点掉卡片，center=({btn['cx']},{btn['cy']})")
    log(f"  本进程完整性={integrity_level()}；抢前台结果={force_foreground(hwnd)} "
        f"（Low 完整性永远抢不到 Medium 窗口，见 README）")
    return False


# ---------------------------------------------------------------- 主循环 ----
def loop(interval: float, do_inject: bool, auto_approve: bool,
         dry_run: bool, tick: float, prompt_file: Path | None = None,
         review: bool = False, allow: list[str] | None = None,
         deny: list[str] | None = None, label: str = "watch") -> None:
    pf = prompt_file or DRIVE_TEXT
    log(f"[{label}] 启动 间隔={interval:.0f}s 注入={do_inject} 自动确认={auto_approve} "
        f"review={review} dry_run={dry_run} 提示词={pf.name} 窗口=「{TITLE}」")
    if allow:
        log(f"[{label}] 白名单：{allow}")
    if deny:
        log(f"[{label}] 黑名单：{deny}")
    last_inject = 0.0
    prev_digest: str | None = None
    settled = 0
    approvals = 0
    fails = 0
    while True:
        time.sleep(tick)
        try:
            hwnd = find_window()
            if hwnd is None or window_rect(hwnd)[0] < -10000:
                # 没开或最小化：自己唤醒，而不是干等（观测量：双击 Ctrl 生效）
                hwnd = wake(quiet=True)
                if hwnd is None:
                    log("窗口不在且唤醒失败，等下一轮")
                    prev_digest, settled = None, 0
                    time.sleep(5)
                    continue
            left, top, width, height = window_rect(hwnd)
            w, h, shot = grab(hwnd)
            marks = landmarks(shot, w, h)

            btn, diag = find_consent(shot, w, h, marks["scan_bottom"])
            if btn:
                if auto_approve:
                    if dry_run:
                        card = read_card(hwnd, btn)
                        log(f"[dry-run] 检测到「确认」发给「{card.get('recipient') or '?'}」"
                            f"内容「{card.get('content') or '?'}」→ 不动手")
                    else:
                        done, _card = handle_confirm(hwnd, btn, shot, w, h,
                                                     review, allow, deny)
                        if done:
                            approvals += 1
                            fails = 0
                        else:
                            fails += 1
                            if fails >= 2:
                                log("连续处理不掉卡片，退避 20 秒（卡片可能已失效或布局变了）")
                                time.sleep(20)
                                fails = 0
                else:
                    log(f"检测到「确认」但未开启自动确认 center=({btn['cx']},{btn['cy']})")
                time.sleep(1.0)
                prev_digest, settled = None, 0
                continue

            busy, _ = is_busy(shot, w, h, marks["send"])
            digest = message_digest(shot, w, h, marks["box"])
            settled = settled + 1 if digest == prev_digest else 0
            prev_digest = digest

            for d in diag:
                log(f"  次要蓝块 {d}")

            if not do_inject:
                continue
            if busy:
                continue
            if settled < 2:
                continue
            if time.time() - last_inject < interval:
                continue
            prompt = load_prompt(pf)
            if dry_run:
                log(f"[dry-run] 会注入：{prompt}")
                last_inject = time.time()
                continue
            inject(hwnd, prompt, marks)
            last_inject = time.time()
            log(f"已注入：{prompt}")
        except Exception as exc:  # noqa: BLE001
            log(f"本轮出错：{exc}")
            time.sleep(3)


def doctor() -> int:
    """体检：能不能找到窗口、抓图、判定、抢前台。全都不动真格。"""
    print(f"本进程完整性        : {integrity_level()}")
    hwnd = wake()
    if hwnd is None:
        print(f"「{TITLE}」窗口      : 叫不出来（企微没运行？双击 Ctrl 也没用）")
        return 2
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    left, top, w, h = window_rect(hwnd)
    print(f"「{TITLE}」窗口      : hwnd={hwnd} pid={pid.value} rect=({left},{top}) {w}x{h}")
    print(f"目标进程完整性      : {integrity_level(pid.value)}")
    if left < -10000:
        print("窗口状态            : 已最小化且还原不了")
        return 3
    try:
        gw, gh, shot = grab(hwnd)
        print(f"抓图                : OK {gw}x{gh}")
    except Exception as exc:  # noqa: BLE001
        print(f"抓图                : 失败 {exc}")
        return 4
    marks = landmarks(shot, gw, gh)
    btn, diag = find_consent(shot, gw, gh, marks["scan_bottom"])
    busy, hits = is_busy(shot, gw, gh, marks["send"])
    print(f"地标来源            : {marks['source']}"
          f"{'' if marks['source'] == 'detected' else '  ← 没认出输入框，用的是旧比例兜底'}")
    if marks["box"]:
        b = marks["box"]
        print(f"  输入容器          : ({b['x0']},{b['y0']})-({b['x1']},{b['y1']}) {b['w']}x{b['h']}")
    print(f"  输入框点击点      : {marks['input']}")
    print(f"  发送键点击点      : {marks['send']}")
    print(f"  消息区点击点      : {marks['message']}")
    print(f"  OCR 裁剪范围      : y {marks['scan_top']}..{marks['scan_bottom']}")
    print(f"确认卡片            : {btn if btn else '当前没有'}")
    print(f"忙碌状态            : {busy} {hits}")
    for d in diag:
        print(f"  次要蓝块          : {d}")
    prev = user32.GetForegroundWindow()
    if prev == hwnd:
        print("抢前台              : 它本来就是前台")
    else:
        ok = force_foreground(hwnd)
        print(f"抢前台              : {'成功' if ok else '失败'}"
              f"（失败通常是完整性不够：Low 抢不到 Medium）")
        if ok:
            user32.SetForegroundWindow(prev)
    return 0


def approve_once(dry_run: bool = False) -> int:
    """单次：找窗口 -> 找「确认」-> 点 -> 再抓一次确认卡片确实消失了。"""
    hwnd = wake()
    if hwnd is None:
        log("叫不出「智能助理」窗口")
        return 2
    left, top, w, h = window_rect(hwnd)
    w, h, shot = grab(hwnd)
    marks = landmarks(shot, w, h)
    btn, diag = find_consent(shot, w, h, marks["scan_bottom"])
    if btn is None:
        log("没有检测到「确认」按钮")
        for d in diag:
            log(f"  次要蓝块 {d}")
        return 1
    log(f"检测到「确认」 center=({btn['cx']},{btn['cy']}) "
        f"area={btn['area']} white={btn['white']} 屏幕=({left + btn['cx']},{top + btn['cy']})")
    if dry_run:
        log("[dry-run] 不点")
        return 0
    ok = approve(hwnd, btn, shot, w, h)
    return 0 if ok else 4


def load_image(path: Path) -> tuple[int, int, bytes]:
    """读一张图成 BGRA。BMP 走内置解析（无依赖）；PNG 等格式需要 Pillow。

    主程序坚持纯标准库，所以 Pillow 只是可选：仓库里的自检样本是 PNG，
    想跑 `check samples/xxx.png` 就装一下 Pillow，否则用 BMP 也一样。
    """
    if path.suffix.lower() == ".bmp":
        return load_bmp(path)
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            f"读 {path.suffix} 需要 Pillow（pip install pillow），或者改用 32 位 BMP"
        ) from None
    im = Image.open(path).convert("RGB")
    w, h = im.size
    rgb = im.tobytes()
    bgra = bytearray(w * h * 4)
    bgra[0::4] = rgb[2::3]
    bgra[1::4] = rgb[1::3]
    bgra[2::4] = rgb[0::3]
    bgra[3::4] = b"\xff" * (w * h)
    return w, h, bytes(bgra)


def check_image(path: Path) -> int:
    w, h, bgra = load_image(path)
    marks = landmarks(bgra, w, h)
    btn, diag = find_consent(bgra, w, h, marks["scan_bottom"])
    busy, hits = is_busy(bgra, w, h, marks["send"])
    print(f"image {path} {w}x{h}")
    print(f"landmarks = source={marks['source']} box={marks['box']} "
          f"input={marks['input']} send={marks['send']}")
    print(f"consent = {btn}")
    print(f"busy    = {busy} {hits}")
    for d in diag:
        print(f"  blob {d}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    # 允许写成 /auto-reply 这种斜杠命令形式
    if argv and argv[0].startswith("/"):
        argv[0] = argv[0][1:]
    parser = argparse.ArgumentParser(
        prog="dayuan",
        description="把企业微信里的「智能助理」（大圆）当成一个命令行工具来用。",
    )
    parser.add_argument(
        "cmd",
        choices=("watch", "auto-reply", "ask", "read", "status", "wake",
                 "approve", "once", "check", "doctor"),
    )
    parser.add_argument("text", nargs="?", help="ask 要发给大圆的话；check 要读的 BMP")
    parser.add_argument("--interval", type=float, default=180,
                        help="watch 注入间隔秒数，默认 180；auto-reply 默认 300")
    parser.add_argument("--tick", type=float, default=1.2, help="巡检周期秒数，默认 1.2")
    parser.add_argument("--timeout", type=float, default=300, help="ask 等回复的上限秒数，默认 300")
    parser.add_argument("--out", help="ask 把回复写进这个文件")
    parser.add_argument("--json", action="store_true", help="status / read 输出 JSON")
    parser.add_argument("--no-inject", action="store_true", help="watch：只自动点确认，不注入提示词")
    parser.add_argument("--no-approve", action="store_true", help="watch：不自动点确认")
    parser.add_argument("--review", action="store_true",
                        help="只审不发：读懂确认卡后点「取消」，不让它真的发出去")
    parser.add_argument("--allow", action="append", default=[],
                        help="白名单（收件人名，可重复）。设了以后只批名单内的")
    parser.add_argument("--deny", action="append", default=[],
                        help="黑名单（收件人名，可重复）。命中就点「取消」")
    parser.add_argument("--once", action="store_true",
                        help="auto-reply：只注入一轮就退出（先看它想干什么）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不动手")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "read":
        return cmd_read(as_json=args.json)
    if args.cmd == "ask":
        if not args.text:
            parser.error("ask 需要给一句要发给大圆的话")
        return cmd_ask(args.text, args.timeout, args.out, args.dry_run)
    if args.cmd == "check":
        if not args.text:
            parser.error("check 需要给一个 BMP 路径")
        return check_image(Path(args.text))
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "wake":
        return cmd_wake(dry_run=args.dry_run)
    if args.cmd == "approve":
        return approve_once(dry_run=args.dry_run)
    if args.cmd == "auto-reply":
        iv = args.interval if args.interval != 180 else 300
        return cmd_auto_reply(iv, args.tick, args.review, args.dry_run,
                              args.allow, args.deny, once=args.once,
                              timeout=args.timeout)
    if args.cmd == "once":
        hwnd = wake()
        if hwnd is None:
            log("叫不出「智能助理」窗口")
            return 2
        inject(hwnd, load_prompt())
        log("已注入一次")
        return 0

    try:
        loop(args.interval, not args.no_inject, not args.no_approve,
             args.dry_run, args.tick, review=args.review,
             allow=args.allow, deny=args.deny)
    except KeyboardInterrupt:
        log("手动停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
