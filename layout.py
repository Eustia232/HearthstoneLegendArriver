# -*- coding: utf-8 -*-
"""屏幕坐标映射层：把 1920×1080 参考坐标换算到实际分辨率。

背景：脚本的全部点击坐标、阶段判定点原本按 1920×1080 全屏硬编码，换分辨率
就整体错位。本模块把「参考坐标 → 实际屏幕坐标」的换算集中到一处：

游戏锚点（ANCHOR_GAME，默认）
    炉石的棋盘 UI 以渲染高度为基准等比缩放、并在屏幕内水平/垂直居中
    （iPad 4:3 与 PC 16:9 同源渲染）。因此对任意分辨率取
        s  = min(W/1920, H/1080)          # 16:9 时即均匀缩放比
        x' = ox + W/2 + (x - 960) * s
        y' = oy + H/2 + (y - 540) * s
    1920×1080 全屏时 s=1、退化为恒等映射 —— 旧行为逐像素不变。
    16:9 分辨率（1366×768 ~ 4K）为精确支持；非 16:9 依赖“棋盘居中”假设，
    属实验性支持，环境自检会给出提示。

screen 锚点（ANCHOR_SCREEN）
    桌面绝对像素，不随游戏缩放：炉石盒子（HSAng）的浮动条/时间线按钮、
    鼠标复位点这类“不属于游戏渲染”的坐标。盒子 UI 位置本就与分辨率无关，
    需要校准时走 ui_config 覆盖（见 config.py）。

布局（GameLayout）描述游戏渲染区在桌面上的矩形（全屏 = 整个主屏），
origin/size 字段为后续“窗口化模式支持”预留：届时用炉石窗口客户区构造。

阶段判定点（STATE_PROBE_POINTS）与盒子区域参考值也登记在这里，作为
get_screen / screen_regions / FSM_action 的唯一来源，避免多处手工同步。
（历史注：原 screen_regions 把选牌判定点写成 (860, 960)，而 get_state 实际
采样 im_opencv[860][960] 即 (x=960, y=860)——确认按钮上的灰色像素；本表
按实际采样点 (960, 860) 修正。）

命令行自检：python layout.py --check  打印检测到的布局与关键点映射。
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass

# ---------------------------------------------------------------- 参考分辨率
# 全部参考坐标（click.py 的手牌表、排布间距、get_screen 判定点等）的基准。
REF_WIDTH = 1920
REF_HEIGHT = 1080
REF_CENTER_X = REF_WIDTH // 2    # 960
REF_CENTER_Y = REF_HEIGHT // 2   # 540

ANCHOR_GAME = "game"      # 随游戏渲染缩放（棋盘/手牌/菜单/判定点）
ANCHOR_SCREEN = "screen"  # 桌面绝对像素（盒子 UI、复位点）

# 换算取整后再加 left_click 自带的 ±2px 抖动，足够吸收亚像素误差。
# 判定点 RGB 容差取 4：状态调色板两两最小通道差为 9（(17,18,19) vs
# (8,18,24)），容差必须小于它才不会互相误判。
PIXEL_TOLERANCE = 4
PIXEL_NEIGHBORHOOD = 1    # 判定点采样半径（3×3 邻域）


@dataclass(frozen=True)
class GameLayout:
    """游戏渲染区在桌面上的矩形（全屏时 origin=0 且 size=主屏分辨率）。"""
    origin_x: int = 0
    origin_y: int = 0
    width: int = REF_WIDTH
    height: int = REF_HEIGHT

    def rect(self) -> tuple[int, int, int, int]:
        return (self.origin_x, self.origin_y, self.width, self.height)

    @property
    def is_reference(self) -> bool:
        return self.rect() == (0, 0, REF_WIDTH, REF_HEIGHT)

    @property
    def scale(self) -> float:
        """棋盘缩放比：窄屏受宽度限制、宽屏受高度限制、16:9 两者相等。"""
        return min(self.width / REF_WIDTH, self.height / REF_HEIGHT)

    # ------------------------------------------------------------ 换算
    def map_point(self, x, y, anchor: str = ANCHOR_GAME) -> tuple[int, int]:
        """参考坐标 → 桌面像素坐标（取整并收敛到布局矩形内）。"""
        if anchor == ANCHOR_SCREEN:
            px, py = int(x), int(y)
        else:
            s = self.scale
            px = self.origin_x + self.width / 2 + (x - REF_CENTER_X) * s
            py = self.origin_y + self.height / 2 + (y - REF_CENTER_Y) * s
            px, py = int(round(px)), int(round(py))
        return (self._clamp(px, self.origin_x, self.origin_x + self.width - 1),
                self._clamp(py, self.origin_y, self.origin_y + self.height - 1))

    def map_region(self, box, anchor: str = ANCHOR_GAME) -> tuple[int, int, int, int]:
        """参考区域 (l, t, r, b) → 桌面像素区域（对角点各映射一次再归整）。"""
        left, top, right, bottom = box
        x0, y0 = self.map_point(left, top, anchor)
        x1, y1 = self.map_point(right, bottom, anchor)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    @staticmethod
    def _clamp(value, low, high) -> int:
        return max(low, min(high, int(value)))


# ---------------------------------------------------------------- 模块级当前布局
# 默认恒等（参考布局）：单元测试与未调用 auto_detect 的路径行为与旧版一致。
_REFERENCE_LAYOUT = GameLayout()
_current_layout: GameLayout | None = None
_detected_size: tuple[int, int] | None = None


def current() -> GameLayout:
    """当前生效布局（未检测过时为恒等的参考布局）。"""
    return _current_layout if _current_layout is not None else _REFERENCE_LAYOUT


def set_layout(new_layout: GameLayout) -> None:
    """显式设置当前布局（测试注入 / 窗口模式预留）。"""
    global _current_layout
    _current_layout = new_layout


def reset_layout() -> None:
    """恢复恒等参考布局。"""
    global _current_layout
    _current_layout = None


def _win32_screen_size() -> tuple[int, int] | None:
    """主屏分辨率（物理像素）；非 Windows 或读取失败返回 None。"""
    try:
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def detected_desktop_size() -> tuple[int, int]:
    """主屏分辨率；读取失败回退参考分辨率（保证测试/非 Windows 可用）。"""
    global _detected_size
    if _detected_size is None:
        _detected_size = _win32_screen_size() or (REF_WIDTH, REF_HEIGHT)
    return _detected_size


def auto_detect() -> GameLayout:
    """按当前主屏分辨率启用映射（FSM/web 启动时调用一次）。"""
    width, height = detected_desktop_size()
    layout = GameLayout(origin_x=0, origin_y=0, width=width, height=height)
    set_layout(layout)
    return layout


def map_point(x, y, anchor: str = ANCHOR_GAME) -> tuple[int, int]:
    """便捷入口：按当前布局换算一个点。"""
    return current().map_point(x, y, anchor)


def map_region(box, anchor: str = ANCHOR_GAME) -> tuple[int, int, int, int]:
    """便捷入口：按当前布局换算一个区域。"""
    return current().map_region(box, anchor)


# ---------------------------------------------------------------- 阶段判定点
# get_screen.get_state() 采样的像素点（参考坐标）。这是唯一登记处；
# screen_regions 的区域框叠加与本文档共用，get_state 经 probe_point 取用。
STATE_PROBE_POINTS = (
    {"key": "probe_main", "label": "主界面/选英雄/匹配判定点",
     "point": (1090, 1070)},
    {"key": "probe_main_alt", "label": "主界面判定点（备用）",
     "point": (705, 305)},
    {"key": "probe_mulligan", "label": "选牌界面判定点",
     "point": (960, 860)},
)


def probe_point(key: str) -> tuple[int, int]:
    """按 key 取判定点并换算到当前布局（换算后同样受钳制）。"""
    for probe in STATE_PROBE_POINTS:
        if probe["key"] == key:
            return map_point(*probe["point"])
    raise KeyError(f"未知的阶段判定点：{key}")


# ---------------------------------------------------------------- 盒子 UI 参考值
# 炉石盒子（HSAng）的屏幕元素：位置取决于盒子窗口而非游戏分辨率，
# 因此按桌面绝对像素（ANCHOR_SCREEN）使用；可在 ui_config.json 覆盖
# （见 config.win_rate_regions / config.timeline_positions）。
# 盒子浮动条“AI胜率 X%”（1920x1080 实测）：主区域 + 放宽的兜底区域。
AI_WIN_RATE_REGION = (110, 8, 270, 48)
AI_WIN_RATE_WIDE_REGION = (95, 0, 300, 60)
# HSAng 左下「时间线」提示按钮中心（1920x1080 实测，见用户截图）：
#   回溯(撤销) ≈ (351, 805)   维持(保留) ≈ (582, 805)
TIMELINE_UNDO_POS = (351, 805)
TIMELINE_KEEP_POS = (582, 805)


# ---------------------------------------------------------------- 判定像素匹配
def sample_matches(pixels, x: int, y: int, expected,
                   tolerance: int = PIXEL_TOLERANCE,
                   radius: int = PIXEL_NEIGHBORHOOD) -> bool:
    """判定点采样：expected 任一变体在 (x,y) 的邻域内逐通道差 ≤ 容差即命中。

    pixels 按 pixels[y][x][:3] 取像素（numpy 数组与嵌套序列都兼容），
    通道顺序沿用 GetBitmapBits 的原始字节序，与旧版逐值比较一致。
    expected 是单个 (c0, c1, c2) 或若干变体的元组。
    """
    if isinstance(expected, tuple) and expected and isinstance(
            expected[0], (tuple, list)):
        variants = tuple(tuple(int(c) for c in variant) for variant in expected)
    else:
        variants = (tuple(int(c) for c in expected),)
    height = len(pixels)
    if height == 0:
        return False
    width = len(pixels[0])
    for dy in range(-radius, radius + 1):
        py = y + dy
        if not 0 <= py < height:
            continue
        row = pixels[py]
        for dx in range(-radius, radius + 1):
            px = x + dx
            if not 0 <= px < width:
                continue
            pixel = row[px][:3]
            for variant in variants:
                if all(abs(int(pixel[ch]) - variant[ch]) <= tolerance
                       for ch in range(3)):
                    return True
    return False


# ---------------------------------------------------------------- 命令行自检
def describe() -> str:
    """人读的布局摘要：检测到的分辨率 + 关键点映射表（--check 用）。"""
    lay = current()
    width, height = detected_desktop_size()
    lines = [f"主屏分辨率：{width}×{height}；"
             f"当前布局：origin=({lay.origin_x},{lay.origin_y}) "
             f"size={lay.width}×{lay.height} scale={lay.scale:.4f}"
             + ("（参考布局，恒等映射）" if lay.is_reference else "")]
    keys = ("手牌 1 号位", "随从 1 号位", "结束回合", "换牌确认",
            "我方英雄", "牌库", "阶段判定点(主)", "阶段判定点(选牌)")
    refs = ((885, 1000), (960, 600), (1550, 500), (960, 850),
            (960, 850), (1635, 640), (1090, 1070), (960, 860))
    for label, (x, y) in zip(keys, refs):
        lines.append(f"  {label:<10} ({x:>4},{y:>4}) → {map_point(x, y)}")
    return "\n".join(lines)


def main() -> int:
    import sys
    auto_detect()
    print(describe())
    if "--check" in sys.argv:
        print("[OK] 布局检测完成（如需在游戏内核对，请用浮窗「校准」）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
