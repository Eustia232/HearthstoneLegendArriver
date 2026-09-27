# -*- coding: utf-8 -*-
"""probe_background 纯逻辑部分的单元测试（可在无 Windows 的机器上运行）。"""
import unittest

import numpy as np

import probe_background as probe


class LparamTests(unittest.TestCase):
    def test_pack_matches_makelparam_layout(self):
        # LOWORD=x, HIWORD=y
        self.assertEqual(probe.pack_lparam(100, 200), (200 << 16) | 100)
        self.assertEqual(probe.pack_lparam(0, 0), 0)
        self.assertEqual(probe.pack_lparam(1895, 1060), (1060 << 16) | 1895)

    def test_round_trip(self):
        for x, y in ((0, 0), (1, 2), (1919, 1079), (885, 1000)):
            ux, uy = probe.unpack_lparam(probe.pack_lparam(x, y))
            self.assertEqual((ux, uy), (x, y))


class ActivationTests(unittest.TestCase):
    def test_pseudo_activate_uses_wm_activate_wa_active(self):
        step = probe.build_pseudo_activate()
        self.assertEqual(step.msg, probe.WM_ACTIVATE)
        self.assertEqual(step.wparam, probe.WA_ACTIVE)
        self.assertEqual(step.lparam, 0)
        self.assertIsNone(step.client)  # 伪激活不移动光标

    def test_prepend_activation_puts_activate_first(self):
        seq = probe.prepend_activation(probe.build_click_sequence(5, 6))
        self.assertEqual(seq[0].msg, probe.WM_ACTIVATE)
        self.assertEqual([s.msg for s in seq[1:]],
                         [probe.WM_MOUSEMOVE, probe.WM_LBUTTONDOWN,
                          probe.WM_LBUTTONUP])


class ClickSequenceTests(unittest.TestCase):
    def test_three_messages_with_client_coord(self):
        seq = probe.build_click_sequence(960, 540)
        self.assertEqual([s.msg for s in seq],
                         [probe.WM_MOUSEMOVE, probe.WM_LBUTTONDOWN,
                          probe.WM_LBUTTONUP])
        self.assertEqual([s.wparam for s in seq],
                         [0, probe.MK_LBUTTON, 0])
        for step in seq:
            self.assertEqual(step.lparam, probe.pack_lparam(960, 540))
            self.assertEqual(step.client, (960, 540))

    def test_button_down_comes_before_up(self):
        seq = probe.build_click_sequence(10, 20)
        msgs = [s.msg for s in seq]
        self.assertLess(msgs.index(probe.WM_LBUTTONDOWN),
                        msgs.index(probe.WM_LBUTTONUP))


class DragSequenceTests(unittest.TestCase):
    def test_sequence_shape_and_endpoints(self):
        seq = probe.build_drag_sequence(100, 900, 400, 300, steps=8)
        # move + down + 8 moves + up
        self.assertEqual(len(seq), 11)
        self.assertEqual(seq[0].msg, probe.WM_MOUSEMOVE)
        self.assertEqual(seq[0].client, (100, 900))
        self.assertEqual(seq[1].msg, probe.WM_LBUTTONDOWN)
        self.assertEqual(seq[-1].msg, probe.WM_LBUTTONUP)
        self.assertEqual(seq[-1].client, (400, 300))

    def test_intermediate_moves_are_continuous_and_button_held(self):
        seq = probe.build_drag_sequence(0, 0, 100, 50, steps=10)
        moves = seq[2:-1]
        for move in moves:
            self.assertEqual(move.msg, probe.WM_MOUSEMOVE)
            self.assertEqual(move.wparam, probe.MK_LBUTTON)  # 按住中
        xs = [probe.unpack_lparam(m.lparam)[0] for m in moves]
        self.assertEqual(xs, list(range(10, 101, 10)))  # 严格递增、无缝

    def test_single_step_drag_is_direct(self):
        seq = probe.build_drag_sequence(0, 0, 50, 50, steps=1)
        self.assertEqual(len(seq), 4)
        self.assertEqual(seq[2].client, (50, 50))


class EscapeSequenceTests(unittest.TestCase):
    def test_keydown_up_with_scan_code(self):
        seq = probe.build_escape_sequence()
        self.assertEqual([s.msg for s in seq],
                         [probe.WM_KEYDOWN, probe.WM_KEYUP])
        self.assertEqual([s.wparam for s in seq],
                         [probe.VK_ESCAPE, probe.VK_ESCAPE])
        # down: repeat=1 | scan(0x01)<<16；up: 另加 transition/previous 位
        self.assertEqual(seq[0].lparam, 0x00010001)
        self.assertEqual(seq[1].lparam, 0xC0010001)


class CoordinateTests(unittest.TestCase):
    def test_identity_at_reference_size(self):
        self.assertEqual(probe.ref_to_client(1895, 1060, 1920, 1080),
                         (1895, 1060))
        self.assertEqual(probe.ref_to_client(960, 540, 1920, 1080), (960, 540))

    def test_uniform_scale_on_16_9_client(self):
        # 1280x720：s=2/3，齿轮 x=640+935*2/3≈1263，y=360+520*2/3≈707
        self.assertEqual(probe.ref_to_client(1895, 1060, 1280, 720),
                         (1263, 707))

    def test_narrow_client_limits_by_width(self):
        # 960x1080：s=0.5，x=480+935/2≈948，y=540+520/2=800
        x, y = probe.ref_to_client(1895, 1060, 960, 1080)
        self.assertEqual((x, y), (948, 800))

    def test_gear_point_uses_bottom_right_ref(self):
        self.assertEqual(probe.gear_point(1920, 1080), (1895, 1060))


class ImageJudgeTests(unittest.TestCase):
    @staticmethod
    def _solid(h, w, value=0):
        return np.full((h, w, 3), value, dtype=np.uint8)

    def test_identical_frames_are_not_alive(self):
        img = self._solid(100, 200, 30)
        self.assertFalse(probe.frames_alive(img, img.copy()))

    def test_animated_frames_are_alive(self):
        a = self._solid(100, 200, 30)
        b = self._solid(100, 200, 90)
        self.assertTrue(probe.frames_alive(a, b))

    def test_frames_alive_rejects_shape_mismatch(self):
        self.assertFalse(probe.frames_alive(self._solid(10, 10),
                                            self._solid(20, 20)))

    def test_black_detection(self):
        self.assertTrue(probe.capture_is_black(self._solid(50, 50, 0)))
        self.assertTrue(probe.capture_is_black(self._solid(50, 50, 3)))
        self.assertFalse(probe.capture_is_black(self._solid(50, 50, 40)))

    def test_center_diff_ignores_border_changes(self):
        a = self._solid(100, 100, 10)
        b = a.copy()
        b[:10, :] = 200  # 只有边框变化
        self.assertEqual(probe.center_diff(a, b), 0.0)
        c = a.copy()
        c[40:60, 40:60] = 200  # 中央变化
        self.assertGreater(probe.center_diff(a, c), 0.0)

    def test_find_yellow_button_centroid(self):
        img = self._solid(400, 600)
        # 在下部中央画一块黄色（BGR: 黄色 B 低 G 高 R 高）
        img[350:380, 280:340] = (30, 190, 230)
        point = probe.find_yellow_button(img)
        self.assertIsNotNone(point)
        bx, by = point
        self.assertTrue(300 <= bx <= 320)  # 质心 ≈ 310
        self.assertTrue(360 <= by <= 370)  # 质心 ≈ 365

    def test_find_yellow_button_none_when_absent(self):
        self.assertIsNone(probe.find_yellow_button(self._solid(400, 600)))

    def test_find_yellow_button_ignores_small_noise(self):
        img = self._solid(400, 600)
        img[300, 300] = (30, 190, 230)  # 单像素噪声
        self.assertIsNone(probe.find_yellow_button(img))


class VerdictTests(unittest.TestCase):
    @staticmethod
    def _round(open_ok, close_ok, esc=None):
        return {"open": {"ok": open_ok}, "close": {"ok": close_ok},
                "keyboard_esc_ok": esc}

    @staticmethod
    def _report(click=None, capture_ok=True):
        captures = {
            "printwindow": {"ok": capture_ok, "black": not capture_ok},
            "printwindow_alive": {"alive": capture_ok},
        }
        return {"captures": captures, "click": click or {}, "drag": None}

    def test_strategy_a_success_is_best(self):
        report = self._report({"post": self._round(True, True, esc=False),
                               "post_flick": self._round(True, True)})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "可行")
        self.assertEqual(verdict["best_transport"], "post")
        self.assertIn("最优", verdict["input_verdict"])
        self.assertIn("未生效", verdict["keyboard_verdict"])

    def test_falls_back_to_flick_when_post_fails(self):
        report = self._report({"post": self._round(False, False),
                               "post_flick": self._round(True, True)})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["best_transport"], "post_flick")
        self.assertIn("瞬移", verdict["input_verdict"])

    def test_all_failed_is_not_feasible(self):
        report = self._report({"post": self._round(False, False),
                               "send": self._round(False, False),
                               "post_flick": self._round(False, False)})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "待排查/不可行")
        self.assertIsNone(verdict["best_transport"])

    def test_black_capture_blocks_verdict_even_if_input_works(self):
        report = self._report({"post": self._round(True, True)},
                              capture_ok=False)
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "待排查/不可行")
        self.assertEqual(verdict["best_transport"], "post")  # 输入侧结论保留

    def test_keyboard_ok_when_any_round_closed_with_esc(self):
        report = self._report({"post": self._round(True, True, esc=True)})
        verdict = probe.compute_verdict(report)
        self.assertIn("可用", verdict["keyboard_verdict"])

    def test_keyboard_unknown_without_open(self):
        report = self._report({})
        verdict = probe.compute_verdict(report)
        self.assertIn("未测得", verdict["keyboard_verdict"])


if __name__ == "__main__":
    unittest.main()
