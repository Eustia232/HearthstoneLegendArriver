# -*- coding: utf-8 -*-
"""炉石传说后台模式可行性探针 v3（通道自校验版）。

三轮实测的结论（详见 docs/background-probe.md 判读表）：
1. 渲染不是瓶颈：前台/失焦/遮挡/幽灵透明的 idle 帧差同为 0.01~0.08，
   菜单打开 = 27~54，游戏在所有状态下都在渲染（大厅空闲动画本来就细微）。
2. 前台合成消息确认被消费；键盘（ESC）不消费（Unity Raw Input，本项目纯鼠标）。
3. 后台消息「一次会话全通、一次会话全灭」。最可信解释：PrintWindow 可能返回
   陈旧帧——单帧状态确认会被骗（残留的选项菜单 + 陈旧大厅帧 → 齿轮被挡 →
   点击被消费但画面 0.0 变化）。

v3 对策：把「确认状态、验证通道、容忍呈现延迟」做成**通道自校验回路**：
- 每个策略轮先清扫残留菜单（黄按钮）→ 点齿轮开菜单 → **多帧观察**确认打开
  → 黄按钮关菜单 → 多帧观察确认回到大厅。「开」「关」双向都有真实状态变化
  作证后，通道才算健康 —— 陈旧帧无法再欺骗判定。
- 每次动作后采样多帧（0.6s 间隔），直接量出呈现延迟，区分「未消费」与「晚呈现」。
- hover-tickle 实验：只发 WM_MOUSEMOVE（不点击），看悬停高亮是否出现在后台
  截图 —— 「合成输入被消费 + 画面按需刷新」的最小用例，决定后端截图前
  是否加输入唤醒步。

用法（在有炉石的 Windows 机器上，管理员终端）：
    uv run --no-project python probe_background.py
    uv run --no-project python probe_background.py --drag   # 追加拖牌探针（练习模式）

输出：probe_out/ 下的截图与 probe_report.json，回传方式见 docs/background-probe.md。
win32 只在 Windows 分支导入，纯逻辑部分可在任意平台单测（tests/test_probe_background.py）。
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
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x2

# ---------------------------------------------------------------- 参考坐标
# 与 click.py / layout.py 同源的 1920x1080 实测值；经 ref_to_client 映射到客户区。
REF_WIDTH = 1920
REF_HEIGHT = 1080
GEAR_REF = (1895, 1060)        # 大厅右下角设置齿轮（click.click_setting 同源）
HAND1_REF = (885, 1000)        # 手牌 1 号位（HAND_CARD_X[1][0]）
BOARD_CENTER_REF = (960, 600)  # 场上随从区中心
LOBBY_HOVER_REF = (950, 320)   # 大厅「对战模式」按钮（click.enter_battle_mode 同源；
                               # hover-tickle 用：悬停高亮明显且不点击无副作用）

# ---------------------------------------------------------------- 判定阈值
MENU_DIFF_THRESHOLD = 10.0   # 中央区域前后平均差 ≥ 此值 → 菜单打开/未关闭
ALIVE_DIFF_THRESHOLD = 0.3   # 相邻帧平均差 ≥ 此值 → 有活帧（仅参考信息；实测大厅
                             # 空闲动画 ≈ 0.05、菜单打开 ≈ 27，旧值 1.0 过严）
BLACK_MEAN_THRESHOLD = 6.0   # 全图均值 < 此值 → 黑帧
HOVER_DIFF_THRESHOLD = 2.0   # 悬停高亮区域差 ≥ 此值 → hover 被消费且画面刷新
GEAR_RETRY_OFFSETS = ((0, 0), (6, 0), (-6, 0), (0, 6), (0, -6))  # 齿轮点击容错网格
OBSERVE_FRAMES = 5           # 每次动作后的观察帧数
OBSERVE_INTERVAL_S = 0.6     # 观察采样间隔

TRANSPORTS = ("post", "send", "post_flick")  # 后台探针策略 A / B / C
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


def build_hover_sequence(x: int, y: int) -> list[InputStep]:
    """纯悬停消息序列（无点击），用于 hover-tickle 实验。"""
    lp = pack_lparam(x, y)
    return [InputStep(WM_MOUSEMOVE, 0, lp, client=(x, y))]


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
    """两帧差异显著 → 画面仍在刷新（仅参考信息：实测大厅空闲动画 ≈0.05）。"""
    if a.shape != b.shape:
        return False
    return mean_abs_diff(a, b) >= ALIVE_DIFF_THRESHOLD


def capture_is_black(img: np.ndarray) -> bool:
    return img.size == 0 or float(img.mean()) < BLACK_MEAN_THRESHOLD


def observe_summary(baseline: np.ndarray, frames: list[np.ndarray],
                    threshold: float = MENU_DIFF_THRESHOLD) -> dict:
    """动作后的长观察判定（纯函数）。

    以 baseline 为基准逐帧算差异，返回：
    - diffs/max_diff：逐帧与最大差异（呈现延迟的直接证据）
    - changed_at：首个差异 ≥ threshold 的帧序号（1 起；None = 未观察到变化）
    - settled：所有帧差异均 < threshold（用于确认「回到大厅」）
    形状不兼容的帧记 None 差异，不让单帧异常毁掉整轮观察。
    """
    diffs: list[float | None] = []
    for frame in frames:
        try:
            diffs.append(round(mean_abs_diff(baseline, frame), 2))
        except ValueError:
            diffs.append(None)
    valid = [d for d in diffs if d is not None]
    max_diff = max(valid) if valid else 0.0
    changed_at = None
    for i, diff in enumerate(diffs):
        if diff is not None and diff >= threshold:
            changed_at = i + 1
            break
    return {"n": len(frames), "diffs": diffs, "max_diff": max_diff,
            "changed_at": changed_at, "changed": changed_at is not None,
            "settled": bool(valid) and max_diff < threshold}


def alive_summary(frames: list[np.ndarray]) -> dict:
    """相邻帧活帧判定（仅参考信息）。"""
    diffs = []
    for i in range(1, len(frames)):
        try:
            diffs.append(round(mean_abs_diff(frames[i - 1], frames[i]), 3))
        except ValueError:
            return {"frames": len(frames), "mean_diffs": None,
                    "max_diff": None, "alive": False}
    max_diff = max(diffs) if diffs else 0.0
    return {"frames": len(frames), "mean_diffs": diffs,
            "max_diff": max_diff, "alive": bool(max_diff >= ALIVE_DIFF_THRESHOLD)}


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
    """按探针结果给出人读结论（纯函数，report 结构见各 run_* 函数）。

    判定优先级：行为证据（自校验回路 > hover 响应 > 前台对照）优先于
    参考信息（alive 帧差）。
    """
    caps = report.get("captures", {})
    pw = caps.get("printwindow", {})
    pw_ok = bool(pw.get("ok")) and not bool(pw.get("black", True))

    fg_post = report.get("click_foreground", {}).get("post", {})
    fg_flick = report.get("click_foreground", {}).get("post_flick", {})
    fg_post_ok = bool(fg_post.get("open", {}).get("ok")) or (
        fg_post.get("user_confirmed") is True)
    fg_flick_ok = bool(fg_flick.get("open", {}).get("ok")) or (
        fg_flick.get("user_confirmed") is True)
    if fg_post_ok:
        foreground_verdict = "前台：纯 PostMessage 合成点击被消费（最优）"
        input_any = True
    elif fg_flick_ok:
        foreground_verdict = "前台：需要真实光标瞬移才被消费（游戏读真实光标）"
        input_any = True
    else:
        foreground_verdict = "前台对照组失败：炉石不消费合成鼠标消息"
        input_any = False

    def round_worked(value: dict | None) -> bool:
        return bool(value and value.get("open", {}).get("ok")
                    and value.get("close", {}).get("ok"))

    click = report.get("click", {})
    best_transport = None
    for transport in TRANSPORTS:
        if round_worked(click.get(transport)):
            best_transport = transport
            break

    tickle = report.get("tickle_test", {})
    tickle_changed = bool(tickle.get("changed"))

    rendering = caps.get("rendering", {})
    alive_states = [key for key, value in rendering.items()
                    if isinstance(value, dict) and value.get("alive")]
    if alive_states:
        rendering_note = f"有活帧的状态：{', '.join(alive_states)}（参考信息）"
    else:
        rendering_note = "各状态 idle 帧差低于参考阈值（大厅空闲动画细微，属正常）"

    if not pw_ok:
        capture_verdict = "PrintWindow 基线不可用（黑帧/失败）：截图通道未过"
    elif best_transport:
        capture_verdict = ("通道自校验通过：开/关菜单双向被后台截图证实，"
                           "截图与输入同时得证")
    elif tickle_changed:
        capture_verdict = ("hover 有响应（后台截图会随输入刷新），"
                           "但开/关自校验回路未通过——看各轮 observe 明细")
    elif input_any:
        capture_verdict = "前台可用、后台开/关回路未通过——看各轮 observe 与截图"
    else:
        capture_verdict = "后台回路未通过且前台对照也失败"

    background_notes = {
        "post": "后台纯 PostMessage 可用：无感后台（最优）",
        "send": "后台 SendMessage 可用",
        "post_flick": "后台需光标瞬移：可用但操作瞬间光标会闪",
    }
    if best_transport is not None:
        background_verdict = background_notes[best_transport]
    elif tickle_changed:
        background_verdict = ("自校验未通过但 hover 有响应：输入通道活着，"
                              "疑似残留菜单/状态机受扰，清理后重跑")
    elif input_any:
        background_verdict = "后台自校验未通过（前台对照通过）：重跑一次再定性"
    else:
        background_verdict = "后台自校验未通过（前台对照也失败）"

    esc_results = [v.get("keyboard_esc_ok") for v in click.values()
                   if isinstance(v, dict)]
    if any(esc_results):
        keyboard_verdict = "PostMessage 键盘（ESC）可用"
    elif any(r is False for r in esc_results):
        keyboard_verdict = "PostMessage 键盘（ESC）未生效（Unity Raw Input，符合预期）"
    else:
        keyboard_verdict = "未测得"

    if not pw_ok:
        overall = "不可行"
        overall_note = "截图基线失败，先解决 PrintWindow（切换窗口化重试）"
    elif best_transport:
        overall = "可行"
        overall_note = "后台输入+截图双向得证，可以进入后台后端实现"
    elif input_any:
        overall = "有条件可行"
        overall_note = "前台可用但后台自校验未过：排查残留菜单后重跑一轮再定性"
    else:
        overall = "不可行"
        overall_note = "合成鼠标消息不被消费，后台输入路线搁置"

    return {
        "overall": overall,
        "overall_note": overall_note,
        "capture_verdict": capture_verdict,
        "foreground_verdict": foreground_verdict,
        "background_verdict": background_verdict,
        "keyboard_verdict": keyboard_verdict,
        "rendering_note": rendering_note,
        "best_transport": best_transport,
        "tickle_changed": tickle_changed,
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


def observe_frames(hwnd: int, n: int = OBSERVE_FRAMES,
                   interval: float = OBSERVE_INTERVAL_S) -> list[np.ndarray]:
    """动作后的多帧采样（PrintWindow；None 帧剔除）。"""
    frames: list[np.ndarray] = []
    for i in range(n):
        img, _ = capture_printwindow(hwnd)
        if img is not None:
            frames.append(img)
        if i < n - 1:
            time.sleep(interval)
    return frames


def set_alpha_ghost(hwnd: int) -> int:
    """把窗口设为近透明（幽灵模式）：加 WS_EX_LAYERED + alpha=3。

    经典后台挂机保活手段：窗口仍被 DWM 合成（可能绕过遮挡暂停渲染），
    但肉眼几乎不可见。返回原始扩展样式供 restore_alpha 还原。
    """
    import win32gui
    original = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
    win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, original | WS_EX_LAYERED)
    win32gui.SetLayeredWindowAttributes(hwnd, 0, 3, LWA_ALPHA)
    return original


def restore_alpha(hwnd: int, original_exstyle: int) -> None:
    """还原幽灵模式：先恢复不透明再摘掉 LAYERED 样式。"""
    import win32gui
    try:
        win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, original_exstyle)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 还原窗口透明样式失败：{exc}（重启炉石即可恢复）")


def _wait_enter(prompt: str) -> None:
    try:
        input(prompt)
    except EOFError:
        print("（非交互环境：跳过等待继续执行）")


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "y"


def _capture_or_none(hwnd: int) -> np.ndarray | None:
    return capture_printwindow(hwnd)[0]


# ---------------------------------------------------------------- 探针流程


def ensure_main_menu(hwnd: int, out_dir: Path, max_rounds: int = 3) -> bool:
    """截图并让用户确认炉石停在大厅（粗确认；权威判定交给自校验回路）。"""
    for _ in range(max_rounds):
        img = _capture_or_none(hwnd)
        save_png(img, out_dir / "state_check.png")
        print("已截图保存到 probe_out/state_check.png（大厅 = 有「对战模式」等"
              "大按钮、右下角齿轮的界面）")
        answer = _ask("截图里炉石是否停在大厅？(y=是 / n=不是，我先去操作) ")
        if answer.startswith("y"):
            return True
        _wait_enter("请把炉石操作到大厅，然后回到这里按回车（我会重新截图确认）")
    print("[WARN] 未确认大厅状态，继续探针（结果请人工复核截图）。")
    return False


def rendering_probe(hwnd: int, out_dir: Path) -> dict:
    """四状态渲染参考信息：前台 / 可见但失焦 / 被遮挡 / 被遮挡+幽灵透明。

    v3 起 alive 只是参考信息（大厅空闲动画细微），行为证据以自校验回路为准。
    """
    print("\n========== 渲染四状态探针（参考信息） ==========")
    print("大厅空闲动画帧差实测 ≈0.05，低于 0.3 阈值属正常；本项仅作记录。")
    result: dict = {}

    print("\n[R1/4] 前台")
    _wait_enter("把 cmd 移开别挡住炉石 → 在这里按回车 → 3 秒内点【任务栏上的炉石图标】"
                "把它切到前台（不要点游戏画面内部）→ 然后手完全离开鼠标")
    time.sleep(3.0)
    frames = observe_frames(hwnd, 3, 0.8)
    result["foreground"] = alive_summary(frames)
    save_png(frames[0] if frames else None, out_dir / "render_foreground.png")
    print(f"  前台：{result['foreground']}")

    print("\n[R2/4] 可见但失焦（模拟你切去干别的事）")
    _wait_enter("点击 cmd 窗口让它成为活跃窗口，但把它摆到【不遮住炉石】的位置"
                " → 按回车 → 手离开鼠标")
    time.sleep(1.0)
    frames = observe_frames(hwnd, 3, 0.8)
    result["visible_unfocused"] = alive_summary(frames)
    save_png(frames[0] if frames else None, out_dir / "render_unfocused.png")
    print(f"  可见但失焦：{result['visible_unfocused']}")

    print("\n[R3/4] 被完全遮挡")
    _wait_enter("把 cmd 拖大（或移动）到【完全盖住】炉石 → 按回车 → 手离开鼠标")
    time.sleep(1.0)
    print(f"  遮挡检测：{'已遮挡' if is_occluded(hwnd) else '未遮挡（请尽量盖严）'}")
    frames = observe_frames(hwnd, 3, 0.8)
    result["occluded"] = alive_summary(frames)
    save_png(frames[0] if frames else None, out_dir / "render_occluded.png")
    print(f"  被遮挡：{result['occluded']}")

    print("\n[R4/4] 被遮挡 + 幽灵透明（ghost-alpha 实验）")
    print("  把炉石窗口设为近透明（alpha=3）——经典后台挂机的保活手段，测完立刻还原。")
    original = set_alpha_ghost(hwnd)
    try:
        frames = observe_frames(hwnd, 3, 0.8)
        result["occluded_alpha"] = alive_summary(frames)
        save_png(frames[0] if frames else None,
                 out_dir / "render_occluded_alpha.png")
        print(f"  遮挡+幽灵透明：{result['occluded_alpha']}")
    finally:
        restore_alpha(hwnd, original)
        print("  窗口透明已还原。")

    print("\n四状态小结（参考信息）：")
    for key in ("foreground", "visible_unfocused", "occluded", "occluded_alpha"):
        print(f"  {key:<18}: alive={result[key]['alive']} "
              f"max_diff={result[key]['max_diff']}")
    return result


def _try_close_menu(hwnd: int, transport: str, baseline: np.ndarray | None,
                    out_dir: Path, tag: str) -> dict:
    """关菜单：黄色按钮点击 → ESC → 人工兜底，返回关闭结果。"""
    close_result: dict = {"ok": False, "via": None}
    menu_img = _capture_or_none(hwnd)
    button = find_yellow_button(menu_img) if menu_img is not None else None
    if button is not None:
        off_x, off_y = window_to_client_offset(hwnd)
        click_at(hwnd, transport, button[0] - off_x, button[1] - off_y)
        frames = observe_frames(hwnd)
        obs = observe_summary(baseline, frames) if baseline is not None else {}
        if obs.get("settled"):
            close_result.update(ok=True, via="yellow_button", observe=obs)
            return close_result
    dispatch_steps(hwnd, build_escape_sequence(), transport)
    frames = observe_frames(hwnd)
    esc_obs = (observe_summary(baseline, frames)
               if baseline is not None else {})
    close_result["esc_closed"] = bool(esc_obs.get("settled"))
    if esc_obs.get("settled"):
        close_result.update(ok=True, via="esc", observe=esc_obs)
        return close_result
    save_png(_capture_or_none(hwnd), out_dir / f"{tag}_stuck_menu.png")
    print("  菜单疑似仍开着：请揭开炉石手动关闭（ESC 或点「完成」），"
          "亲眼看到回到大厅后重新盖好。")
    _wait_enter("  处理好后按回车继续")
    close_result["manual"] = True
    return close_result


def foreground_input_probe(hwnd: int, out_dir: Path) -> dict:
    """前台对照组：炉石在前台、手不离鼠标，测合成消息是否被消费。

    若前台都不被消费 → Unity 丢弃合成消息，后台输入路线直接判死；
    若前台可用 → 后台轮的失败才是「遮挡/状态」相关，值得继续攻关。
    """
    print("\n========== 前台输入对照组 ==========")
    result: dict = {}
    _wait_enter("F1 确认炉石在前台且完整可见；把光标移到【炉石窗口以外】"
                "（桌面或 cmd 上）停住 → 按回车 → 之后手完全离开鼠标")
    baseline = _capture_or_none(hwnd)
    save_png(baseline, out_dir / "fg_baseline.png")
    cw, ch = client_size(hwnd)
    gx, gy = gear_point(cw, ch)
    print(f"  目标：右下角齿轮（客户区 {gx},{gy}）。请在炉石上观察结果。")

    for transport in ("post", "post_flick"):
        click_at(hwnd, transport, gx, gy)
        frames = observe_frames(hwnd, 3, 0.6)
        obs = (observe_summary(baseline, frames)
               if baseline is not None else {})
        measured = bool(obs.get("changed"))
        save_png(frames[0] if frames else None,
                 out_dir / f"fg_{transport}_after.png")
        confirmed = _ask(f"  [{transport}] max_diff={obs.get('max_diff')}。"
                         "肉眼看到炉石弹出选项菜单了吗？(y/n) ").startswith("y")
        entry = {
            "transport": transport,
            "observe": obs,
            "measured_open": measured,
            "user_confirmed": bool(confirmed),
        }
        if measured or confirmed:
            print("  合成点击在前台生效！尝试自动关闭菜单…")
            entry["close"] = _try_close_menu(hwnd, transport, baseline,
                                             out_dir, f"fg_{transport}")
            result[transport] = entry
            break
        result[transport] = entry
        print(f"  [{transport}] 前台未生效。")
    return result


def _sweep_stuck_menu(hwnd: int, transport: str, out_dir: Path,
                      tag: str) -> dict | None:
    """开局清扫：若截图里发现黄色「完成」按钮（疑似菜单卡开），点击关闭并观察。

    这是 v2 教训的直接对策：残留菜单 + 陈旧大厅帧曾把整轮判定带偏。
    """
    img = _capture_or_none(hwnd)
    if img is None:
        return None
    button = find_yellow_button(img)
    if button is None:
        return None
    off_x, off_y = window_to_client_offset(hwnd)
    click_at(hwnd, transport, button[0] - off_x, button[1] - off_y)
    frames = observe_frames(hwnd)
    obs = observe_summary(img, frames)
    save_png(frames[0] if frames else None, out_dir / f"{tag}_sweep_after.png")
    print(f"  [清扫] 发现疑似残留菜单（黄按钮 图内 {button}）→ 已尝试关闭："
          f"max_diff={obs['max_diff']} settled={obs['settled']}")
    return obs


def click_probe_round(hwnd: int, transport: str, out_dir: Path,
                      tag: str) -> dict:
    """v3 自校验轮：清扫 → 点齿轮开菜单（多帧确认）→ 黄按钮关菜单（多帧确认）
    → 重开一次测 ESC。开与关双向被截图证实，通道才算这一轮可用。"""
    print(f"\n---------- 后台策略 {TRANSPORT_LABELS[transport]}（自校验轮） ----------")
    cw, ch = client_size(hwnd)
    gx, gy = gear_point(cw, ch)
    result: dict = {
        "transport": transport,
        "gear_point": [gx, gy],
        "open": {"ok": False, "attempts": [], "observe": None},
        "close": {"ok": False, "via": None, "observe": None},
        "keyboard_esc_ok": None,
    }
    result["sweep"] = _sweep_stuck_menu(hwnd, transport, out_dir, tag)

    baseline = _capture_or_none(hwnd)
    if baseline is None:
        print("[FAIL] 拿不到基线截图，跳过本策略。")
        return result
    save_png(baseline, out_dir / f"{tag}_baseline.png")
    print(f"  目标：设置齿轮（客户区 {gx},{gy}）；每次动作后观察 "
          f"{OBSERVE_FRAMES} 帧 × {OBSERVE_INTERVAL_S}s。手不要碰鼠标。")

    opened = False
    for dx, dy in GEAR_RETRY_OFFSETS:
        click_at(hwnd, transport, gx + dx, gy + dy)
        frames = observe_frames(hwnd)
        obs = observe_summary(baseline, frames)
        result["open"]["attempts"].append({"offset": [dx, dy], **obs})
        suffix = (f"（第 {obs['changed_at']} 帧出现，≈"
                  f"{(obs['changed_at'] - 1) * OBSERVE_INTERVAL_S:.1f}s）"
                  if obs["changed"] else "")
        print(f"  齿轮点击 {dx:+d},{dy:+d} → 逐帧差异 {obs['diffs']} → "
              f"{'菜单打开' if obs['changed'] else '未变化'}{suffix}")
        if obs["changed"]:
            opened = True
            result["open"].update(ok=True, observe=obs)
            menu_frame = frames[obs["changed_at"] - 1]
            save_png(menu_frame, out_dir / f"{tag}_menu_open.png")
            break
    result["open"]["ok"] = opened
    if not opened:
        print("  [结论] 多帧观察下整场无变化：合成点击未被消费，"
              "或齿轮被残留菜单挡住（看 sweep 字段）。")
        return result

    menu_img = _capture_or_none(hwnd)
    button = find_yellow_button(menu_img) if menu_img is not None else None
    off_x, off_y = window_to_client_offset(hwnd)
    if button is not None:
        print(f"  找到黄色按钮（图内 {button}），点击关闭…")
        click_at(hwnd, transport, button[0] - off_x, button[1] - off_y)
        frames = observe_frames(hwnd)
        obs = observe_summary(baseline, frames)
        result["close"] = {"ok": obs["settled"], "via": "yellow_button",
                           "observe": obs, "button": list(button)}
        print(f"  关闭后逐帧差异 {obs['diffs']} → "
              f"{'回到大厅' if obs['settled'] else '未回到大厅'}")
        save_png(frames[0] if frames else None,
                 out_dir / f"{tag}_after_close1.png")

    if result["close"]["ok"]:
        print("  再开一次菜单测 ESC 键…")
        click_at(hwnd, transport, gx, gy)
        frames = observe_frames(hwnd)
        reopened = observe_summary(baseline, frames)
        if reopened["changed"]:
            dispatch_steps(hwnd, build_escape_sequence(), transport)
            frames = observe_frames(hwnd)
            esc_obs = observe_summary(baseline, frames)
            result["keyboard_esc_ok"] = esc_obs["settled"]
            print(f"  ESC 关闭：{'成功' if esc_obs['settled'] else '未生效'}")
            if not esc_obs["settled"]:
                result["close"] = {"ok": False, "via": None, "observe": esc_obs}
        else:
            print("  重开菜单失败，ESC 未测。")
    else:
        dispatch_steps(hwnd, build_escape_sequence(), transport)
        frames = observe_frames(hwnd)
        esc_obs = observe_summary(baseline, frames)
        result["keyboard_esc_ok"] = esc_obs["settled"]
        if esc_obs["settled"]:
            result["close"] = {"ok": True, "via": "esc", "observe": esc_obs}
            print("  ESC 关闭成功。")
        else:
            print("  ESC 未生效。")
            _try_close_menu(hwnd, transport, baseline, out_dir, tag)
    return result


def tickle_probe(hwnd: int, out_dir: Path, transport: str = "post") -> dict:
    """hover-tickle：只发 WM_MOUSEMOVE（不点击），看悬停高亮是否出现在后台截图。

    证明「合成输入被消费 + 画面随输入刷新」的最小用例：
    - 成立 → 后端可在每次截图前用无害 hover 唤醒画面（可靠截图的关键机制）；
    - 不成立 → 后端必须依赖「动作后延时取帧」。
    """
    print("\n========== hover-tickle 实验 ==========")
    base = _capture_or_none(hwnd)
    if base is None:
        print("[FAIL] 拿不到基线截图。")
        return {"error": "no_baseline"}
    save_png(base, out_dir / "tickle_baseline.png")
    cw, ch = client_size(hwnd)
    hx, hy = ref_to_client(*LOBBY_HOVER_REF, cw, ch)
    dispatch_steps(hwnd, build_hover_sequence(hx, hy), transport)
    frames = observe_frames(hwnd, 3, 0.5)
    full = observe_summary(base, frames)
    h, w = base.shape[:2]
    box = (max(0, hx - w // 8), max(0, hy - h // 10),
           min(w, hx + w // 8), min(h, hy + h // 10))
    region_diffs: list[float] = []
    for frame in frames:
        try:
            region_diffs.append(round(mean_abs_diff(base, frame, box), 2))
        except ValueError:
            continue
    region_max = max(region_diffs) if region_diffs else 0.0
    changed = bool(full["changed"] or region_max >= HOVER_DIFF_THRESHOLD)
    if frames:
        save_png(frames[-1], out_dir / "tickle_after.png")
    result = {"transport": transport, "hover_point": [hx, hy],
              "full": full, "region_diffs": region_diffs,
              "region_max_diff": region_max, "changed": changed}
    print(f"  hover ({hx},{hy}) → 整帧 {full['diffs']} / 按钮区域 {region_diffs} → "
          f"{'输入被消费且画面刷新' if changed else '未见响应'}")
    return result


def drag_probe(hwnd: int, out_dir: Path, transport: str = "post") -> dict:
    """拖牌探针：练习模式里把手牌 1 号位拖到场上中心，前后截图人工比对。"""
    print("\n========== 拖牌探针 ==========")
    print("请先进入练习模式，确保手牌里至少有一张可打出的随从牌。")
    _wait_enter("准备好后按回车开始拖拽（Ctrl+C 退出）")
    cw, ch = client_size(hwnd)
    x1, y1 = ref_to_client(*HAND1_REF, cw, ch)
    x2, y2 = ref_to_client(*BOARD_CENTER_REF, cw, ch)
    print(f"拖拽：手牌1号位 ({x1},{y1}) → 场上中心 ({x2},{y2})，通道 {transport}")
    before = _capture_or_none(hwnd)
    save_png(before, out_dir / "drag_before.png")
    dispatch_steps(hwnd, prepend_activation(
        build_drag_sequence(x1, y1, x2, y2)), transport)
    time.sleep(1.2)
    frames = observe_frames(hwnd, 3, 0.6)
    after = frames[-1] if frames else None
    save_png(after, out_dir / "drag_after.png")
    result: dict = {"transport": transport, "from": [x1, y1], "to": [x2, y2]}
    if before is not None and after is not None:
        h, w = before.shape[:2]
        box = (max(0, x2 - w // 5), max(0, y2 - h // 8),
               min(w, x2 + w // 5), min(h, y2 + h // 8))
        diff = mean_abs_diff(before, after, box)
        result["board_region_diff"] = round(diff, 2)
        result["board_box"] = list(box)
        result["needs_human_confirm"] = True
        print(f"场上区域前后差异：{diff:.1f}（是否真的放下随从请比对 "
              "drag_before/after.png 人工确认）")
    else:
        result["board_region_diff"] = None
        print("[WARN] 截图缺失，无法计算差异，请直接看保存的图片。")
    return result


def _capture_entry(img: np.ndarray | None, ok: bool, label: str) -> dict:
    return {
        "ok": bool(ok and img is not None),
        "black": bool(img is None or capture_is_black(img)),
        "size": list(img.shape[:2]) if img is not None else None,
        "label": label,
    }


def print_summary(report: dict) -> None:
    verdict = report["verdict"]
    print("\n========== 探针结论 ==========")
    print(f"渲染参考：{verdict['rendering_note']}")
    for transport in TRANSPORTS:
        rnd = report.get("click", {}).get(transport)
        if rnd is None:
            continue
        open_obs = rnd.get("open", {}).get("observe") or {}
        close_obs = rnd.get("close", {}).get("observe") or {}
        print(f"后台 {transport.upper()}: open={rnd['open']['ok']} "
              f"(max_diff={open_obs.get('max_diff')}) "
              f"close={rnd['close']['ok']} "
              f"(max_diff={close_obs.get('max_diff')}) "
              f"esc={rnd.get('keyboard_esc_ok')}")
    tickle = report.get("tickle_test")
    if tickle:
        print(f"hover-tickle：changed={tickle.get('changed')} "
              f"region_max={tickle.get('region_max_diff')}")
    print(f"截图/输入通道：{verdict['capture_verdict']}")
    print(f"前台对照：{verdict['foreground_verdict']}")
    print(f"后台通道：{verdict['background_verdict']}")
    print(f"键盘：{verdict['keyboard_verdict']}")
    print(f"总体：{verdict['overall']} —— {verdict['overall_note']}")
    print("把 probe_report.json 与 probe_out/*.png 按手册回传即可。")


def main(argv: list[str] | None = None) -> int:
    _ensure_windows()
    parser = argparse.ArgumentParser(description="炉石后台模式可行性探针 v3")
    parser.add_argument("--drag", action="store_true",
                        help="追加拖牌探针（需先进练习模式）")
    parser.add_argument("--strategies", default=",".join(TRANSPORTS),
                        help="要测的后台点击策略，逗号分隔：post,send,post_flick")
    parser.add_argument("--out", default="probe_out", help="输出目录")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    hwnd = find_hs_hwnd()
    if not hwnd:
        print("[FAIL] 没找到炉石窗口：请先启动游戏。")
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

    report: dict = {"meta": meta, "captures": {}, "click_foreground": {},
                    "tickle_test": None, "click": {}, "drag": None}

    print("\n主菜单/大厅 = 登录后有「对战模式 / 竞技场 / 酒馆战棋」大按钮、"
          "右下角齿轮的界面。")
    _wait_enter("请先登录炉石并停在大厅，然后在这里按回车")
    ensure_main_menu(hwnd, out_dir)

    pw_img, pw_ok = capture_printwindow(hwnd)
    report["captures"]["printwindow"] = {
        "ok": bool(pw_ok and pw_img is not None),
        "black": bool(pw_img is None or capture_is_black(pw_img)),
        "size": list(pw_img.shape[:2]) if pw_img is not None else None,
    }
    report["captures"]["display_mode"] = meta["display_mode"]
    report["captures"]["window_dc"] = _capture_entry(*capture_window_dc(hwnd),
                                                     "窗口DC BitBlt")
    report["captures"]["desktop"] = _capture_entry(capture_desktop(), True,
                                                   "桌面BitBlt")
    save_png(pw_img, out_dir / "capture_printwindow.png")
    save_png(capture_window_dc(hwnd)[0], out_dir / "capture_window_dc.png")
    save_png(capture_desktop(), out_dir / "capture_desktop.png")

    report["captures"]["rendering"] = rendering_probe(hwnd, out_dir)

    report["click_foreground"] = foreground_input_probe(hwnd, out_dir)

    _wait_enter("\n后台实验开始：请把 cmd 完全盖住炉石 → 回车")
    report["tickle_test"] = tickle_probe(hwnd, out_dir)

    transports = [t.strip() for t in args.strategies.split(",") if t.strip()]
    for i, transport in enumerate(transports):
        if transport not in TRANSPORTS:
            print(f"[WARN] 未知策略 {transport}，跳过")
            continue
        if i > 0:
            _wait_enter("继续下一策略：确认 cmd 仍盖住炉石 → 回车")
        tag = f"click_{transport}"
        report["click"][transport] = click_probe_round(hwnd, transport,
                                                       out_dir, tag)

    if args.drag:
        report["drag"] = drag_probe(hwnd, out_dir)

    _sweep_stuck_menu(hwnd, "post", out_dir, "final")
    print("\n收尾检查（防残留菜单骗过下一轮确认）：")
    _wait_enter("请揭开炉石看一眼：是否停在大厅且没有残留菜单？"
                "（若有请手动关闭）→ 回车")

    report["verdict"] = compute_verdict(report)
    report_path = out_dir / "probe_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n报告已写入：{report_path}")
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
