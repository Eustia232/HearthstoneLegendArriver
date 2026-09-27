# -*- coding: utf-8 -*-
"""炉石传说后台模式可行性探针。

回答两个只有实机才能回答的问题：
1. 炉石窗口被遮挡时，PrintWindow / 窗口 DC / 桌面 BitBlt 三种截图各能否拿到活帧？
2. 炉石（Unity）是否消费 PostMessage / SendMessage 的合成鼠标消息？
   点击落点是消息坐标还是真实光标位置？拖拽能否用消息序列完成？键盘（ESC）呢？

点击探针的三种策略（均可后台执行，区别在注入通道）：
    A  post        纯 PostMessage，不碰真实光标（最优解，若成功即"无感后台"）
    B  send        SendMessage（同步注入）
    C  post_flick  PostMessage + SetCursorPos 瞬移再还原（游戏查真实光标时的兜底）

动作选无风险的「点主菜单设置齿轮打开选项菜单 → 点黄色"完成"按钮关闭 → 再开一次
→ 按 ESC 关闭」，全部由前后截图差异自动判定，不进对局、不改任何设置。

用法（在有炉石的 Windows 机器上，管理员终端）：
    python probe_background.py             # 截图 + 点击探针
    python probe_background.py --drag      # 追加拖牌探针（需先进练习模式）
    python probe_background.py --strategies A,C

输出：probe_out/ 下的截图与 probe_report.json，回传方式见 docs/background-probe.md。

实现说明：消息序列配方取自 MaaFramework MessageInput.cpp/InputUtils.h
（WM_ACTIVATE 伪激活 → WM_MOUSEMOVE → WM_LBUTTONDOWN/UP，lParam=客户区坐标）；
win32 只在 Windows 分支导入，纯逻辑部分可在任意平台单测
（tests/test_probe_background.py）。
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------- Win32 消息常量
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
VK_ESCAPE = 0x1B
PW_RENDERFULLCONTENT = 0x00000002  # Win8.1+，可截被遮挡的 D3D 窗口

# ---------------------------------------------------------------- 参考坐标
# 与 click.py / layout.py 同源的 1920x1080 实测值；经 ref_to_client 映射到客户区。
REF_WIDTH = 1920
REF_HEIGHT = 1080
GEAR_REF = (1895, 1060)        # 主菜单右下角设置齿轮（click.click_setting 同源）
HAND1_REF = (885, 1000)        # 手牌 1 号位（HAND_CARD_X[1][0]）
BOARD_CENTER_REF = (960, 600)  # 场上随从区中心

# ---------------------------------------------------------------- 判定阈值
MENU_DIFF_THRESHOLD = 10.0   # 中央区域前后平均差 ≥ 此值 → 菜单打开/未关闭
ALIVE_DIFF_THRESHOLD = 1.0   # 间隔 1s 两帧平均差 ≥ 此值 → 画面仍在渲染（活帧）
BLACK_MEAN_THRESHOLD = 6.0   # 全图均值 < 此值 → 黑帧
GEAR_RETRY_OFFSETS = ((0, 0), (6, 0), (-6, 0), (0, 6), (0, -6))  # 齿轮点击容错网格

TRANSPORTS = ("post", "send", "post_flick")  # 探针策略 A / B / C
TRANSPORT_LABELS = {
    "post": "A 纯 PostMessage（不碰真实光标）",
    "send": "B SendMessage（同步注入）",
    "post_flick": "C PostMessage + 光标瞬移还原",
}


# ================================================================ 纯逻辑部分
# 以下函数不依赖 win32，可在任意平台单元测试。


@dataclass(frozen=True)
class InputStep:
    """一条待投递的窗口消息。

    cursor 仅瞬移模式使用：发送本条前把真实光标挪到该【屏幕】坐标，
    整个序列结束后由 dispatch 恢复原位。client 记录消息对应的客户区坐标，
    供瞬移模式换算屏幕坐标用。
    """

    msg: int
    wparam: int
    lparam: int
    delay_ms: int = 0
    client: tuple[int, int] | None = None


def pack_lparam(x: int, y: int) -> int:
    """MAKELPARAM：低 16 位 x（客户区坐标），高 16 位 y。"""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def unpack_lparam(lparam: int) -> tuple[int, int]:
    return (int(lparam) & 0xFFFF, (int(lparam) >> 16) & 0xFFFF)


def ref_to_client(ref_x: float, ref_y: float,
                  client_w: int, client_h: int) -> tuple[int, int]:
    """1920x1080 参考坐标 → 客户区坐标（与 layout.GameLayout 同一套换算）。

    棋盘 UI 以渲染高度为基准等比缩放、并在客户区内居中，故取
    s = min(w/1920, h/1080) 后向中心缩放；1920x1080 客户区时为恒等。
    """
    s = min(client_w / REF_WIDTH, client_h / REF_HEIGHT)
    x = client_w / 2 + (ref_x - REF_WIDTH / 2) * s
    y = client_h / 2 + (ref_y - REF_HEIGHT / 2) * s
    return int(round(x)), int(round(y))


def gear_point(client_w: int, client_h: int) -> tuple[int, int]:
    return ref_to_client(*GEAR_REF, client_w, client_h)


def build_pseudo_activate() -> InputStep:
    """WM_ACTIVATE 伪激活：让目标窗口以为自己被激活，但不改变前台窗口。

    配方来自 MaaFramework InputUtils.send_activate_message。
    """
    return InputStep(WM_ACTIVATE, WA_ACTIVE, 0, delay_ms=10)


def prepend_activation(steps: list[InputStep]) -> list[InputStep]:
    return [build_pseudo_activate(), *steps]


def build_click_sequence(x: int, y: int, hold_ms: int = 60) -> list[InputStep]:
    """后台点击消息序列：WM_MOUSEMOVE → WM_LBUTTONDOWN → WM_LBUTTONUP。"""
    lp = pack_lparam(x, y)
    return [
        InputStep(WM_MOUSEMOVE, 0, lp, delay_ms=15, client=(x, y)),
        InputStep(WM_LBUTTONDOWN, MK_LBUTTON, lp, delay_ms=hold_ms, client=(x, y)),
        InputStep(WM_LBUTTONUP, 0, lp, client=(x, y)),
    ]


def build_drag_sequence(x1: int, y1: int, x2: int, y2: int,
                        steps: int = 12, step_ms: int = 20,
                        hold_ms: int = 60) -> list[InputStep]:
    """后台拖拽消息序列：按下后插值连续 WM_MOUSEMOVE，再松开。

    真实拖拽的中间移动是关键：只发 down/up 不发 move，多数引擎不会
    识别成拖放。步进插值让每条 WM_MOUSEMOVE 的落点连续。
    """
    seq = [
        InputStep(WM_MOUSEMOVE, 0, pack_lparam(x1, y1),
                  delay_ms=15, client=(x1, y1)),
        InputStep(WM_LBUTTONDOWN, MK_LBUTTON, pack_lparam(x1, y1),
                  delay_ms=hold_ms, client=(x1, y1)),
    ]
    for i in range(1, steps + 1):
        t = i / steps
        xi = int(round(x1 + (x2 - x1) * t))
        yi = int(round(y1 + (y2 - y1) * t))
        seq.append(InputStep(WM_MOUSEMOVE, MK_LBUTTON, pack_lparam(xi, yi),
                             delay_ms=step_ms, client=(xi, yi)))
    seq.append(InputStep(WM_LBUTTONUP, 0, pack_lparam(x2, y2),
                         client=(x2, y2)))
    return seq


def build_escape_sequence() -> list[InputStep]:
    """ESC 键消息序列。lParam 带硬件扫描码（ESC=0x01）：
    down = repeat(1) | scan<<16；up 另加 transition/previous 状态位。
    MaaNTE PR#350 证实不带 wScan 的合成按键会被引擎丢弃。
    """
    return [
        InputStep(WM_KEYDOWN, VK_ESCAPE, 0x00010001, delay_ms=50),
        InputStep(WM_KEYUP, VK_ESCAPE, 0xC0010001),
    ]


def mean_abs_diff(a: np.ndarray, b: np.ndarray,
                  box: tuple[int, int, int, int] | None = None) -> float:
    """两张 BGR 图的平均绝对差；box=(left, top, right, bottom) 时只算该区域。"""
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    if box is not None:
        left, top, right, bottom = box
        a = a[top:bottom, left:right]
        b = b[top:bottom, left:right]
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


def center_box(shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    """图中央 50% 区域 (l, t, r, b)——选项菜单面板出现的位置。"""
    h, w = shape[0], shape[1]
    return (w // 4, h // 4, w * 3 // 4, h * 3 // 4)


def center_diff(before: np.ndarray, after: np.ndarray) -> float:
    return mean_abs_diff(before, after, center_box(before.shape))


def frames_alive(a: np.ndarray, b: np.ndarray) -> bool:
    """两帧差异显著 → 目标窗口被遮挡时仍在渲染（活帧）。"""
    if a.shape != b.shape:
        return False
    return mean_abs_diff(a, b) >= ALIVE_DIFF_THRESHOLD


def capture_is_black(img: np.ndarray) -> bool:
    return img.size == 0 or float(img.mean()) < BLACK_MEAN_THRESHOLD


def find_yellow_button(img: np.ndarray, y_min_ratio: float = 0.55,
                       x_band: tuple[float, float] = (0.2, 0.8),
                       min_pixels: int = 300) -> tuple[int, int] | None:
    """在图像下部找黄色大色块（选项菜单的“完成”按钮）质心。

    只用 numpy 阈值分割，不依赖 cv2；色块总像素不足 min_pixels 视为未找到。
    """
    h, w = img.shape[:2]
    y0 = int(h * y_min_ratio)
    x0 = int(w * x_band[0])
    x1 = int(w * x_band[1])
    roi = img[y0:, x0:x1]
    if roi.size == 0:
        return None
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    mask = (r > 170) & (g > 130) & ((r - b) > 70) & ((g - b) > 50)
    if int(mask.sum()) < min_pixels:
        return None
    ys, xs = np.nonzero(mask)
    return (int(round(float(xs.mean()) + x0)),
            int(round(float(ys.mean()) + y0)))


def compute_verdict(report: dict) -> dict:
    """按探针结果给出人读结论（纯函数，report 结构见 run_* 函数）。"""
    captures = report.get("captures", {})
    pw = captures.get("printwindow", {})
    alive = captures.get("printwindow_alive", {})
    capture_ok = (bool(pw.get("ok"))
                  and not bool(pw.get("black", True))
                  and bool(alive.get("alive")))

    def round_worked(value: dict | None) -> bool:
        return bool(value and value.get("open", {}).get("ok")
                    and value.get("close", {}).get("ok"))

    click = report.get("click", {})
    best_transport = None
    for transport in TRANSPORTS:
        if round_worked(click.get(transport)):
            best_transport = transport
            break
    notes = {
        "post": "纯 PostMessage 可用：最优后台方案，无需移动真实光标",
        "send": "SendMessage 可用：同步注入，后台可用",
        "post_flick": "仅瞬移方案可用：后台可跑，但每次操作真实光标会闪动",
    }
    if best_transport is not None:
        input_verdict = notes[best_transport]
    elif any(v.get("open", {}).get("ok") for v in click.values()
             if isinstance(v, dict)):
        input_verdict = "点击能让菜单打开但未能自动关闭：部分可用，看截图人工判读"
    else:
        input_verdict = "三种策略均未成功：消息注入路线未通过"

    esc_results = [v.get("keyboard_esc_ok") for v in click.values()
                   if isinstance(v, dict)]
    if any(esc_results):
        keyboard_verdict = "PostMessage 键盘（ESC）可用"
    elif any(r is False for r in esc_results):
        keyboard_verdict = "PostMessage 键盘（ESC）未生效（与 UE/Raw Input 类引擎一致）"
    else:
        keyboard_verdict = "未测得（没有成功打开过菜单）"

    overall = "可行" if capture_ok and best_transport else "待排查/不可行"
    return {
        "overall": overall,
        "capture_ok": capture_ok,
        "best_transport": best_transport,
        "input_verdict": input_verdict,
        "keyboard_verdict": keyboard_verdict,
    }


# ================================================================ Windows 执行部分
# 以下函数依赖 pywin32，只在 Windows 上调用。


def _ensure_windows() -> None:
    if not IS_WINDOWS:
        print("[FAIL] 本探针必须在有炉石的 Windows 机器上运行。")
        raise SystemExit(2)


def find_hs_hwnd() -> int:
    """按标题找炉石窗口（与 get_screen.get_HS_hwnd 同一套标题）。"""
    import win32gui
    for title in ("炉石传说", "《爐石戰記》", "Hearthstone"):
        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            return hwnd
    return 0


def window_to_client_offset(hwnd: int) -> tuple[int, int]:
    """窗口矩形原点 → 客户区原点的偏移（窗口化时为边框+标题栏尺寸）。"""
    import win32gui
    wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
    csl, cst = win32gui.ClientToScreen(hwnd, (0, 0))
    return csl - wl, cst - wt


def client_size(hwnd: int) -> tuple[int, int]:
    import win32gui
    _, _, cr, cb = win32gui.GetClientRect(hwnd)
    return cr, cb


def is_occluded(hwnd: int) -> bool:
    """客户区中心点最顶层窗口不是炉石 → 被遮挡。"""
    import win32con
    import win32gui
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    hit = win32gui.WindowFromPoint(((l + r) // 2, (t + b) // 2))
    if not hit:
        return False
    root = win32gui.GetAncestor(hit, win32con.GA_ROOT)
    return root != hwnd


def window_meta(hwnd: int) -> dict:
    import ctypes
    import win32api
    import win32gui
    wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
    cw, ch = client_size(hwnd)
    screen_w = win32api.GetSystemMetrics(0)
    screen_h = win32api.GetSystemMetrics(1)
    fullscreen_like = (wl, wt) == (0, 0) and (cw, ch) == (screen_w, screen_h)
    dpi = 0
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:
        pass
    admin = False
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass
    return {
        "title": win32gui.GetWindowText(hwnd),
        "class": win32gui.GetClassName(hwnd),
        "window_rect": [wl, wt, wr, wb],
        "client_size": [cw, ch],
        "screen_size": [screen_w, screen_h],
        "display_mode": ("fullscreen_or_borderless" if fullscreen_like
                         else "windowed"),
        "minimized": bool(win32gui.IsIconic(hwnd)),
        "dpi": dpi,
        "is_admin": admin,
        "is_foreground": win32gui.GetForegroundWindow() == hwnd,
        "occluded": is_occluded(hwnd),
        "python": sys.version.split()[0],
        "windows": platform.win32_ver()[0],
    }


def _print_window(hwnd: int, hdc: int, flags: int) -> bool:
    import ctypes
    import win32gui
    try:
        return bool(win32gui.PrintWindow(hwnd, hdc, flags))
    except AttributeError:  # 老版 pywin32 无封装，走 ctypes
        return bool(ctypes.windll.user32.PrintWindow(hwnd, hdc, flags))


def _grab_window(hwnd: int, use_printwindow: bool
                 ) -> tuple[np.ndarray | None, bool]:
    """按窗口矩形抓图。PrintWindow 走 DWM 拿窗口表面（遮挡可用）；
    窗口 DC BitBlt 是对照组（D3D 内容遮挡时通常黑屏/陈旧）。"""
    import win32con
    import win32gui
    import win32ui
    hwnd_dc = mfc_dc = save_dc = bmp = None
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = max(1, r - l), max(1, b - t)
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = True
        if use_printwindow:
            ok = _print_window(hwnd, save_dc.GetSafeHdc(),
                               PW_RENDERFULLCONTENT)
        else:
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        width = info["bmWidth"]
        height = abs(info["bmHeight"])
        img = np.frombuffer(bits, dtype=np.uint8)
        expected = width * height * 4
        if img.size < expected:
            return None, False
        img = img[:expected].reshape(height, width, 4)[:, :, :3].copy()  # BGRA→BGR
        return img, ok
    except Exception as exc:  # noqa: BLE001 —— 探针要扛住一切抓图异常
        print(f"[WARN] 抓图失败（{'PrintWindow' if use_printwindow else '窗口DC'}）：{exc}")
        return None, False
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:  # noqa: BLE001
            pass


def capture_printwindow(hwnd: int) -> tuple[np.ndarray | None, bool]:
    return _grab_window(hwnd, use_printwindow=True)


def capture_window_dc(hwnd: int) -> tuple[np.ndarray | None, bool]:
    return _grab_window(hwnd, use_printwindow=False)


def capture_desktop() -> np.ndarray | None:
    """整屏 BitBlt（与项目 catch_screen 同一套，作对照）。"""
    import win32api
    import win32con
    import win32gui
    import win32ui
    hwin = hwnd_dc = mfc_dc = save_dc = bmp = None
    try:
        w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        hwin = win32gui.GetDesktopWindow()
        hwnd_dc = win32gui.GetWindowDC(hwin)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)
        img = np.frombuffer(bits, dtype=np.uint8).reshape(h, w, 4)
        return img[:, :, :3].copy()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 桌面抓图失败：{exc}")
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if hwnd_dc is not None and hwin is not None:
                win32gui.ReleaseDC(hwin, hwnd_dc)
        except Exception:  # noqa: BLE001
            pass


def save_png(img: np.ndarray | None, path: Path) -> str | None:
    if img is None:
        return None
    from PIL import Image
    Image.fromarray(img[:, :, ::-1]).save(path)  # BGR→RGB
    return str(path)


def client_to_screen_pt(hwnd: int, x: int, y: int) -> tuple[int, int]:
    import win32gui
    return win32gui.ClientToScreen(hwnd, (int(x), int(y)))


def dispatch_steps(hwnd: int, steps: list[InputStep], transport: str) -> None:
    """按通道投递消息序列；post_flick 会在每条消息前把真实光标瞬移到
    对应屏幕坐标，序列结束后恢复原位（WithCursorPos 思路）。"""
    import win32api
    import win32gui
    flick = transport.endswith("_flick")
    post = transport.startswith("post")
    saved_cursor = None
    if flick:
        saved_cursor = win32api.GetCursorPos()
    try:
        for step in steps:
            if flick and step.client is not None:
                win32api.SetCursorPos(client_to_screen_pt(hwnd, *step.client))
            if post:
                win32gui.PostMessage(hwnd, step.msg, step.wparam, step.lparam)
            else:
                win32gui.SendMessage(hwnd, step.msg, step.wparam, step.lparam)
            if step.delay_ms:
                time.sleep(step.delay_ms / 1000.0)
    finally:
        if saved_cursor is not None:
            try:
                win32api.SetCursorPos(saved_cursor)
            except Exception:  # noqa: BLE001
                pass


def click_at(hwnd: int, transport: str, x: int, y: int) -> None:
    dispatch_steps(hwnd, prepend_activation(build_click_sequence(x, y)),
                   transport)


def _wait_enter(prompt: str) -> None:
    try:
        input(prompt)
    except EOFError:
        print("（非交互环境：跳过等待继续执行）")


# ---------------------------------------------------------------- 探针流程


def capture_probe(hwnd: int, out_dir: Path) -> dict:
    """截图探针：三种方式 ×（遮挡状态 + 1s 间隔两帧）→ 是否活帧。"""
    print("\n========== 截图探针 ==========")
    result: dict = {"occluded": is_occluded(hwnd)}
    if not result["occluded"]:
        print("当前炉石未被遮挡。为了测「被遮挡时能否截到」：")
        _wait_enter("请把任意其他窗口完全盖住炉石，然后回到这里按回车（直接回车=不遮挡硬测）")
        result["occluded"] = is_occluded(hwnd)
    print(f"遮挡状态：{'已遮挡' if result['occluded'] else '未遮挡（结果仅供参考）'}")

    pw1, ok1 = capture_printwindow(hwnd)
    time.sleep(1.0)
    pw2, _ = capture_printwindow(hwnd)
    wdc, ok2 = capture_window_dc(hwnd)
    desk = capture_desktop()

    result["printwindow"] = _capture_entry(pw1, ok1, "PrintWindow")
    result["window_dc"] = _capture_entry(wdc, ok2, "窗口DC BitBlt")
    result["desktop"] = _capture_entry(desk, True, "桌面BitBlt")

    save_png(pw1, out_dir / "capture_printwindow_1.png")
    save_png(pw2, out_dir / "capture_printwindow_2.png")
    save_png(wdc, out_dir / "capture_window_dc.png")
    save_png(desk, out_dir / "capture_desktop.png")

    if pw1 is not None and pw2 is not None:
        diff = mean_abs_diff(pw1, pw2)
        result["printwindow_alive"] = {
            "mean_diff": round(diff, 3),
            "alive": frames_alive(pw1, pw2),
        }
    else:
        result["printwindow_alive"] = {"mean_diff": None, "alive": False}

    print(f"PrintWindow   ：{result['printwindow']}")
    print(f"窗口 DC       ：{result['window_dc']}")
    print(f"桌面 BitBlt   ：{result['desktop']}")
    print(f"遮挡下仍在渲染（活帧）：{result['printwindow_alive']}")
    if result["printwindow"].get("black"):
        print("[提示] PrintWindow 全黑：独占全屏模式常见。请把炉石切到"
              "「窗口化」（或无边框）后重跑本探针——后台模式本来就需要窗口化。")
    return result


def _capture_entry(img: np.ndarray | None, ok: bool, label: str) -> dict:
    return {
        "ok": bool(ok and img is not None),
        "black": bool(img is None or capture_is_black(img)),
        "size": list(img.shape[:2]) if img is not None else None,
        "label": label,
    }


def click_probe_round(hwnd: int, transport: str, out_dir: Path,
                      tag: str) -> dict:
    """一次策略回合：开菜单（点击测试1）→ 黄色按钮关闭（点击测试2）
    → 再开一次 → ESC 关闭（键盘测试）→ 全失败则请求人工关闭。"""
    print(f"\n---------- 点击策略 {TRANSPORT_LABELS[transport]} ----------")
    cw, ch = client_size(hwnd)
    gx, gy = gear_point(cw, ch)
    result: dict = {
        "transport": transport,
        "gear_point": [gx, gy],
        "open": {"ok": False, "attempts": []},
        "close": {"ok": False, "via": None},
        "keyboard_esc_ok": None,
        "left_open": False,
    }
    print(f"目标：设置齿轮（客户区坐标 {gx},{gy}）；真实光标请停在屏幕角落不要动。")

    baseline = capture_printwindow(hwnd)[0]
    if baseline is None:
        print("[FAIL] 连基线截图都拿不到，跳过本策略。")
        return result
    save_png(baseline, out_dir / f"{tag}_baseline.png")

    opened = False
    for dx, dy in GEAR_RETRY_OFFSETS:
        click_at(hwnd, transport, gx + dx, gy + dy)
        time.sleep(0.8)
        img = capture_printwindow(hwnd)[0]
        diff = center_diff(baseline, img) if img is not None else -1.0
        hit = diff >= MENU_DIFF_THRESHOLD
        result["open"]["attempts"].append(
            {"offset": [dx, dy], "center_diff": round(diff, 2), "menu_open": hit})
        print(f"  齿轮点击偏移 {dx:+d},{dy:+d} → 中央差异 {diff:.1f} → "
              f"{'菜单已打开' if hit else '未打开'}")
        if hit:
            opened = True
            save_png(img, out_dir / f"{tag}_menu_open.png")
            break
    result["open"]["ok"] = opened
    if not opened:
        print(f"[结论] 策略 {transport.upper()} 的点击未生效（或没点中齿轮）——"
              "若你肉眼看到菜单其实开了，说明判定阈值偏保守，请在回传时说明。")
        return result

    # ---- 关闭测试 1：黄色“完成”按钮（更大的点击目标，顺带验证坐标换算）
    menu_img = capture_printwindow(hwnd)[0]
    button = find_yellow_button(menu_img) if menu_img is not None else None
    off_x, off_y = window_to_client_offset(hwnd)
    if button is not None:
        bx_img, by_img = button
        bx, by = bx_img - off_x, by_img - off_y
        print(f"  找到黄色按钮（图内 {bx_img},{by_img} → 客户区 {bx},{by}），点击关闭")
        click_at(hwnd, transport, bx, by)
        time.sleep(0.8)
        img = capture_printwindow(hwnd)[0]
        closed = (img is not None
                  and center_diff(baseline, img) < MENU_DIFF_THRESHOLD)
        result["close"] = {"ok": closed, "via": "yellow_button",
                           "button_image": [bx_img, by_img],
                           "button_client": [bx, by]}
        print(f"  关闭{'成功' if closed else '失败'}（黄色按钮）")
        save_png(img, out_dir / f"{tag}_after_close1.png")
    else:
        print("  未在下部找到黄色“完成”按钮色块，改用 ESC 关闭。")

    # ---- 关闭测试 2：ESC（键盘通道；若关闭测试 1 已成功则再开一次菜单）
    if result["close"]["ok"]:
        print("  再开一次菜单用于测试 ESC 键…")
        opened2 = _open_menu(hwnd, transport, baseline, gx, gy, result)
        if not opened2:
            result["keyboard_esc_ok"] = None
        else:
            dispatch_steps(hwnd, build_escape_sequence(), transport)
            time.sleep(0.8)
            img = capture_printwindow(hwnd)[0]
            esc_closed = (img is not None
                          and center_diff(baseline, img) < MENU_DIFF_THRESHOLD)
            result["keyboard_esc_ok"] = esc_closed
            print(f"  ESC 关闭菜单：{'成功 → 后台键盘可用' if esc_closed else '未生效'}")
            save_png(img, out_dir / f"{tag}_after_esc.png")
            if not esc_closed:
                _ensure_closed(hwnd, transport, baseline, result, tag, out_dir)
    else:
        # 菜单还开着：先试 ESC（顺带测键盘），再兜底
        dispatch_steps(hwnd, build_escape_sequence(), transport)
        time.sleep(0.8)
        img = capture_printwindow(hwnd)[0]
        esc_closed = (img is not None
                      and center_diff(baseline, img) < MENU_DIFF_THRESHOLD)
        result["keyboard_esc_ok"] = esc_closed
        print(f"  ESC 关闭菜单：{'成功 → 后台键盘可用' if esc_closed else '未生效'}")
        save_png(img, out_dir / f"{tag}_after_esc.png")
        if not esc_closed:
            _ensure_closed(hwnd, transport, baseline, result, tag, out_dir)
    return result


def _open_menu(hwnd: int, transport: str, baseline: np.ndarray,
               gx: int, gy: int, result: dict) -> bool:
    """再开一次菜单（第二次点击测试），成功返回 True。"""
    for dx, dy in GEAR_RETRY_OFFSETS[:3]:
        click_at(hwnd, transport, gx + dx, gy + dy)
        time.sleep(0.8)
        img = capture_printwindow(hwnd)[0]
        if img is not None and center_diff(baseline, img) >= MENU_DIFF_THRESHOLD:
            return True
    print("  第二次打开菜单失败（不影响本轮已记录的结论）。")
    return False


def _ensure_closed(hwnd: int, transport: str, baseline: np.ndarray,
                   result: dict, tag: str, out_dir: Path) -> None:
    """关闭兜底：黄色按钮重试一次，仍失败则请用户手动关闭。"""
    menu_img = capture_printwindow(hwnd)[0]
    button = find_yellow_button(menu_img) if menu_img is not None else None
    if button is not None:
        off_x, off_y = window_to_client_offset(hwnd)
        click_at(hwnd, transport, button[0] - off_x, button[1] - off_y)
        time.sleep(0.8)
    img = capture_printwindow(hwnd)[0]
    if img is not None and center_diff(baseline, img) >= MENU_DIFF_THRESHOLD:
        result["left_open"] = True
        print("  [提示] 菜单疑似仍开着：请手动关闭（ESC 或点“完成”）后回车。")
        _wait_enter("  关好后按回车继续")
        img = capture_printwindow(hwnd)[0]
        if img is not None and center_diff(baseline, img) < MENU_DIFF_THRESHOLD:
            print("  已确认回到主菜单。")
    save_png(img, out_dir / f"{tag}_final_state.png")


def drag_probe(hwnd: int, out_dir: Path, transport: str = "post") -> dict:
    """拖牌探针：练习模式里把手牌 1 号位拖到场上中心，前后截图人工比对。"""
    print("\n========== 拖牌探针 ==========")
    print("请先进入练习模式，确保手牌里至少有一张可打出的随从牌。")
    _wait_enter("准备好后按回车开始拖拽（Ctrl+C 退出）")
    cw, ch = client_size(hwnd)
    x1, y1 = ref_to_client(*HAND1_REF, cw, ch)
    x2, y2 = ref_to_client(*BOARD_CENTER_REF, cw, ch)
    print(f"拖拽：手牌1号位 ({x1},{y1}) → 场上中心 ({x2},{y2})，通道 {transport}")
    before = capture_printwindow(hwnd)[0]
    save_png(before, out_dir / "drag_before.png")
    dispatch_steps(hwnd, prepend_activation(
        build_drag_sequence(x1, y1, x2, y2)), transport)
    time.sleep(1.2)
    after = capture_printwindow(hwnd)[0]
    save_png(after, out_dir / "drag_after.png")
    result: dict = {"transport": transport,
                    "from": [x1, y1], "to": [x2, y2]}
    if before is not None and after is not None:
        h, w = before.shape[:2]
        box = (max(0, x2 - w // 5), max(0, y2 - h // 8),
               min(w, x2 + w // 5), min(h, y2 + h // 8))
        diff = mean_abs_diff(before, after, box)
        result["board_region_diff"] = round(diff, 2)
        result["board_box"] = list(box)
        print(f"场上区域前后差异：{diff:.1f}（>0 说明有变化，是否真的放下随从"
              "请比对 drag_before/after.png 人工确认）")
        result["needs_human_confirm"] = True
    else:
        result["board_region_diff"] = None
        print("[WARN] 截图缺失，无法计算差异，请直接看保存的图片。")
    return result


def print_summary(report: dict) -> None:
    verdict = report["verdict"]
    print("\n========== 探针结论 ==========")
    caps = report.get("captures", {})
    print(f"截图（PrintWindow 遮挡下活帧）："
          f"{'PASS' if verdict['capture_ok'] else 'FAIL'} "
          f"（black={caps.get('printwindow', {}).get('black')}, "
          f"alive={caps.get('printwindow_alive', {}).get('alive')}, "
          f"mode={caps.get('display_mode')}）")
    for transport in TRANSPORTS:
        rnd = report.get("click", {}).get(transport)
        if rnd is None:
            continue
        print(f"点击 {transport.upper()}: open={rnd['open']['ok']} "
              f"close={rnd['close']['ok']} esc={rnd['keyboard_esc_ok']}")
    print(f"输入结论：{verdict['input_verdict']}")
    print(f"键盘结论：{verdict['keyboard_verdict']}")
    print(f"总体：{verdict['overall']}（best_transport={verdict['best_transport']}）")
    print("把 probe_report.json 与 probe_out/*.png 按手册回传即可。")


def main(argv: list[str] | None = None) -> int:
    _ensure_windows()
    parser = argparse.ArgumentParser(description="炉石后台模式可行性探针")
    parser.add_argument("--drag", action="store_true",
                        help="追加拖牌探针（需先进练习模式）")
    parser.add_argument("--strategies", default=",".join(TRANSPORTS),
                        help="要测的点击策略，逗号分隔：post,send,post_flick")
    parser.add_argument("--out", default="probe_out", help="输出目录")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    hwnd = find_hs_hwnd()
    if not hwnd:
        print("[FAIL] 没找到炉石窗口：请先启动游戏并停在主菜单。")
        return 2
    meta = window_meta(hwnd)
    if meta["minimized"]:
        print("[FAIL] 炉石处于最小化状态：请还原窗口后重跑（最小化不在支持范围）。")
        return 2
    print("========== 窗口信息 ==========")
    for key, value in meta.items():
        print(f"  {key}: {value}")
    if not meta["is_admin"]:
        print("[提示] 当前不是管理员权限：若炉石以管理员运行，消息注入会被系统拦截。")

    report: dict = {"meta": meta, "captures": {}, "click": {}, "drag": None}

    print("\n请把炉石停在【主菜单】。")
    _wait_enter("按回车开始探针（Ctrl+C 随时退出）")
    report["captures"] = capture_probe(hwnd, out_dir)

    transports = [t.strip() for t in args.strategies.split(",") if t.strip()]
    for i, transport in enumerate(transports):
        if transport not in TRANSPORTS:
            print(f"[WARN] 未知策略 {transport}，跳过")
            continue
        if i > 0:
            _wait_enter("继续下一策略前，确认炉石停在主菜单，按回车继续")
        tag = f"click_{transport}"
        report["click"][transport] = click_probe_round(hwnd, transport,
                                                       out_dir, tag)

    if args.drag:
        report["drag"] = drag_probe(hwnd, out_dir)

    report["verdict"] = compute_verdict(report)
    report_path = out_dir / "probe_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n报告已写入：{report_path}")
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
