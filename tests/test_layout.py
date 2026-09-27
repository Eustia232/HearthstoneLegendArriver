# -*- coding: utf-8 -*-
"""坐标映射层 layout：恒等性、等比缩放、锚点、判定点表与像素匹配。

本文件只依赖标准库（sample_matches 的像素参数用嵌套列表即可构造），
在任意平台都可运行；不触发 Windows API（auto_detect 的兜底行为在此验证）。
"""

import unittest

import layout


def make_layout(w, h, ox=0, oy=0):
    return layout.GameLayout(origin_x=ox, origin_y=oy, width=w, height=h)


class IdentityTests(unittest.TestCase):
    """1920×1080 全屏是参考布局：映射必须是恒等，保证旧行为逐像素不变。"""

    def setUp(self):
        layout.reset_layout()

    def test_default_layout_is_reference(self):
        self.assertEqual((0, 0, 1920, 1080), layout.current().rect())

    def test_reference_points_map_to_themselves(self):
        points = ((960, 540), (0, 0), (1919, 1079), (1635, 640),
                  (885, 1000), (1550, 500), (1895, 1060), (70, 60))
        for x, y in points:
            self.assertEqual((x, y), layout.map_point(x, y), (x, y))

    def test_reference_region_maps_to_itself(self):
        self.assertEqual((110, 8, 270, 48),
                         layout.map_region((110, 8, 270, 48)))


class GameAnchorScalingTests(unittest.TestCase):
    """16:9 分辨率：等比缩放，中心点对中心点。"""

    def test_1440p_scales_by_four_thirds(self):
        lay = make_layout(2560, 1440)
        self.assertEqual((1280, 720), lay.map_point(960, 540))
        self.assertEqual((2067, 667), lay.map_point(1550, 500))
        self.assertEqual((93, 80), lay.map_point(70, 60))
        self.assertEqual((2527, 1413), lay.map_point(1895, 1060))

    def test_1440p_edges_stay_on_edges(self):
        lay = make_layout(2560, 1440)
        self.assertEqual((0, 0), lay.map_point(0, 0))

    def test_768p_scales_down(self):
        lay = make_layout(1366, 768)
        self.assertEqual((683, 384), lay.map_point(960, 540))
        self.assertEqual((1103, 356), lay.map_point(1550, 500))

    def test_board_spacing_scales_with_the_game(self):
        """随从横排 140px 间距在 1440p 应放大为约 187px。"""
        lay = make_layout(2560, 1440)
        left = lay.map_point(960 - 140, 600)
        center = lay.map_point(960, 600)
        self.assertEqual(187, center[0] - left[0])


class NonSixteenNineTests(unittest.TestCase):
    """非 16:9（实验性）：取 min 缩放比，棋盘以屏幕中心锚定。"""

    def test_16_10_is_width_limited_and_centered(self):
        lay = make_layout(1920, 1200)
        self.assertEqual(1.0, lay.scale)
        self.assertEqual((960, 600), lay.map_point(960, 540))
        self.assertEqual((960, 1060), lay.map_point(960, 1000))

    def test_ultrawide_is_height_limited_and_centered(self):
        lay = make_layout(3440, 1440)
        self.assertEqual(4 / 3, lay.scale)
        self.assertEqual((1720, 720), lay.map_point(960, 540))


class OriginTests(unittest.TestCase):
    """布局带偏移（为窗口模式预留）：整体平移。"""

    def test_offset_shifts_every_game_point(self):
        lay = make_layout(1920, 1080, ox=100, oy=50)
        self.assertEqual((1060, 590), lay.map_point(960, 540))
        self.assertEqual((100, 50), lay.map_point(0, 0))


class ScreenAnchorTests(unittest.TestCase):
    """screen 锚点 = 桌面绝对像素（盒子 UI、复位点），不随游戏缩放。"""

    def test_screen_point_is_identity_at_any_resolution(self):
        lay = make_layout(2560, 1440)
        self.assertEqual((70, 60), lay.map_point(70, 60, layout.ANCHOR_SCREEN))
        self.assertEqual((110, 8), lay.map_point(
            110, 8, layout.ANCHOR_SCREEN))

    def test_screen_region_is_identity_at_any_resolution(self):
        lay = make_layout(2560, 1440)
        self.assertEqual((95, 0, 300, 60),
                         lay.map_region((95, 0, 300, 60),
                                        layout.ANCHOR_SCREEN))


class RegionMappingTests(unittest.TestCase):
    def test_1440p_region_scales(self):
        lay = make_layout(2560, 1440)
        self.assertEqual((147, 11, 360, 64),
                         lay.map_region((110, 8, 270, 48)))

    def test_region_stays_well_formed_after_rounding(self):
        lay = make_layout(1366, 768)
        left, top, right, bottom = lay.map_region((860, 810, 1060, 890))
        self.assertLess(left, right)
        self.assertLess(top, bottom)

    def test_region_is_clamped_to_the_layout(self):
        """越出布局矩形的部分被收敛回屏幕内（防点击/截图落到屏幕外）。"""
        lay = make_layout(1366, 768)
        left, top, right, bottom = lay.map_region((1800, 1000, 2500, 1400))
        self.assertLessEqual(right, 1365)
        self.assertLessEqual(bottom, 767)
        self.assertLess(left, right)
        self.assertLess(top, bottom)


class ProbeTableTests(unittest.TestCase):
    def test_probe_points_are_documented(self):
        keys = [p["key"] for p in layout.STATE_PROBE_POINTS]
        self.assertEqual(["probe_main", "probe_main_alt", "probe_mulligan"],
                         keys)
        points = {p["key"]: p["point"] for p in layout.STATE_PROBE_POINTS}
        self.assertEqual((1090, 1070), points["probe_main"])
        self.assertEqual((705, 305), points["probe_main_alt"])
        # get_state 实际采样的是 (x=960, y=860)：确认按钮上的灰色像素。
        self.assertEqual((960, 860), points["probe_mulligan"])

    def test_probe_point_maps_through_current_layout(self):
        layout.set_layout(make_layout(2560, 1440))
        try:
            # (960, 860) 在 1440p：x=1280, y=720+320*4/3=1146.67→1147
            self.assertEqual((1280, 1147), layout.probe_point("probe_mulligan"))
        finally:
            layout.reset_layout()

    def test_probe_point_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            layout.probe_point("nope")


class ModuleStateTests(unittest.TestCase):
    def setUp(self):
        layout.reset_layout()

    def tearDown(self):
        layout.reset_layout()

    def test_set_layout_changes_mapping_and_reset_restores(self):
        layout.set_layout(make_layout(2560, 1440))
        self.assertEqual((1280, 720), layout.map_point(960, 540))
        layout.reset_layout()
        self.assertEqual((960, 540), layout.map_point(960, 540))


class AutoDetectTests(unittest.TestCase):
    def setUp(self):
        layout.reset_layout()

    def tearDown(self):
        layout.reset_layout()

    def test_non_windows_falls_back_to_reference(self):
        """无 Windows API（CI/Linux）时回退参考分辨率，绝不抛异常。"""
        size = layout.detected_desktop_size()
        self.assertEqual((1920, 1080), size)

    def test_auto_detect_returns_a_layout(self):
        lay = layout.auto_detect()
        self.assertEqual(lay.width, layout.detected_desktop_size()[0])
        self.assertEqual(lay.height, layout.detected_desktop_size()[1])


class BoxConstantsTests(unittest.TestCase):
    def test_box_regions_are_reference_pixels(self):
        self.assertEqual((110, 8, 270, 48), layout.AI_WIN_RATE_REGION)
        self.assertEqual((95, 0, 300, 60), layout.AI_WIN_RATE_WIDE_REGION)
        self.assertEqual((351, 805), layout.TIMELINE_UNDO_POS)
        self.assertEqual((582, 805), layout.TIMELINE_KEEP_POS)


class SampleMatchesTests(unittest.TestCase):
    def setUp(self):
        # 5x5 常数画面：每像素 (B, G, R) 顺序与 GetBitmapBits 一致。
        self.pixels = [[(23, 52, 105, 255)] * 5 for _ in range(5)]

    def test_exact_hit(self):
        self.assertTrue(layout.sample_matches(
            self.pixels, 2, 2, (23, 52, 105)))

    def test_neighborhood_hit(self):
        self.pixels[2][2] = (200, 200, 200, 255)   # 中心被抗锯齿破坏
        self.assertTrue(layout.sample_matches(
            self.pixels, 2, 2, (23, 52, 105)))

    def test_small_drift_within_tolerance(self):
        self.pixels[2][2] = (25, 54, 107, 255)     # 每通道 +2
        self.assertTrue(layout.sample_matches(
            self.pixels, 2, 2, (23, 52, 105)))

    def test_drift_beyond_tolerance_misses(self):
        self.pixels = [[(17, 18, 19, 255)] * 5 for _ in range(5)]
        # 与 CHOOSING_HERO 的 (8, 18, 24) 最大通道差 9 > 容差 4，必须可区分
        self.assertFalse(layout.sample_matches(
            self.pixels, 2, 2, (8, 18, 24)))

    def test_accepts_any_of_the_expected_variants(self):
        self.assertTrue(layout.sample_matches(
            self.pixels, 2, 2, ((23, 52, 105), (20, 51, 104))))
        self.pixels = [[(20, 51, 104, 255)] * 5 for _ in range(5)]
        self.assertTrue(layout.sample_matches(
            self.pixels, 2, 2, ((23, 52, 105), (20, 51, 104))))

    def test_far_out_of_bounds_point_misses(self):
        # 超出采样半径的越界点必须判不匹配（贴边 ±1 仍允许采样到屏内像素）。
        self.assertFalse(layout.sample_matches(
            self.pixels, -5, 0, (23, 52, 105)))
        self.assertFalse(layout.sample_matches(
            self.pixels, 0, 99, (23, 52, 105)))


if __name__ == "__main__":
    unittest.main()
