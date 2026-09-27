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

    def test_hover_sequence_is_a_single_mousemove(self):
        seq = probe.build_hover_sequence(123, 456)
        self.assertEqual(len(seq), 1)
        self.assertEqual(seq[0].msg, probe.WM_MOUSEMOVE)
        self.assertEqual(seq[0].wparam, 0)
        self.assertEqual(seq[0].client, (123, 456))


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

    def test_real_machine_resolution(self):
        # 实机：2048x1152，s≈1.0667
        self.assertEqual(probe.ref_to_client(1895, 1060, 2048, 1152),
                         (2021, 1131))


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

    def test_subtle_idle_animation_below_calibrated_threshold(self):
        # 实测大厅空闲动画帧差 ≈0.05（少数像素轻微变化）；校准后的阈值 0.3
        # 应把它判为「无变化」，而菜单打开（≈27）为「有变化」——差两个数量级
        a = self._solid(100, 200, 30)
        subtle = a.copy()
        subtle[30:70, 80:120] = 33  # 1600/20000 像素 +3 → 均值差 0.24
        self.assertLess(probe.mean_abs_diff(a, subtle), probe.ALIVE_DIFF_THRESHOLD)
        self.assertFalse(probe.frames_alive(a, subtle))
        self.assertTrue(probe.frames_alive(a, self._solid(100, 200, 90)))

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


class AliveSummaryTests(unittest.TestCase):
    def test_static_frames_are_not_alive(self):
        frames = [np.full((50, 60, 3), 20, dtype=np.uint8)] * 3
        summary = probe.alive_summary(frames)
        self.assertEqual(summary["max_diff"], 0.0)
        self.assertFalse(summary["alive"])

    def test_animated_frames_are_alive(self):
        f1 = np.full((50, 60, 3), 20, dtype=np.uint8)
        f2 = np.full((50, 60, 3), 80, dtype=np.uint8)
        summary = probe.alive_summary([f1, f2, f1])
        self.assertEqual(summary["max_diff"], 60.0)
        self.assertTrue(summary["alive"])

    def test_shape_mismatch_reports_not_alive(self):
        f1 = np.zeros((50, 60, 3), dtype=np.uint8)
        f2 = np.zeros((40, 60, 3), dtype=np.uint8)
        summary = probe.alive_summary([f1, f2])
        self.assertFalse(summary["alive"])
        self.assertIsNone(summary["mean_diffs"])


class ObserveSummaryTests(unittest.TestCase):
    """v3 核心：动作后多帧观察（呈现延迟证据 + settled 判定）。"""

    @staticmethod
    def _frame(value):
        return np.full((40, 40, 3), value, dtype=np.uint8)

    def test_change_detected_with_frame_index(self):
        baseline = self._frame(30)
        frames = [self._frame(30), self._frame(90), self._frame(90)]
        obs = probe.observe_summary(baseline, frames)
        self.assertTrue(obs["changed"])
        self.assertEqual(obs["changed_at"], 2)  # 第 2 帧才出现 → 呈现延迟 1 个间隔
        self.assertEqual(obs["max_diff"], 60.0)
        self.assertFalse(obs["settled"])

    def test_settled_when_back_to_baseline(self):
        baseline = self._frame(30)
        frames = [self._frame(31), self._frame(30), self._frame(30)]
        obs = probe.observe_summary(baseline, frames)
        self.assertFalse(obs["changed"])
        self.assertTrue(obs["settled"])

    def test_no_change_when_all_far_from_threshold(self):
        baseline = self._frame(30)
        obs = probe.observe_summary(baseline, [self._frame(31), self._frame(30)])
        self.assertFalse(obs["changed"])
        self.assertTrue(obs["settled"])

    def test_shape_mismatch_frame_becomes_none_diff(self):
        baseline = self._frame(30)
        frames = [np.zeros((10, 10, 3), dtype=np.uint8), self._frame(90)]
        obs = probe.observe_summary(baseline, frames)
        self.assertIsNone(obs["diffs"][0])
        self.assertEqual(obs["diffs"][1], 60.0)
        self.assertTrue(obs["changed"])
        self.assertEqual(obs["changed_at"], 2)

    def test_empty_frames(self):
        obs = probe.observe_summary(self._frame(30), [])
        self.assertEqual(obs["n"], 0)
        self.assertEqual(obs["max_diff"], 0.0)
        self.assertFalse(obs["changed"])
        self.assertFalse(obs["settled"])

    def test_custom_threshold(self):
        baseline = self._frame(30)
        frames = [self._frame(35)]  # 差异 5
        self.assertFalse(probe.observe_summary(baseline, frames)["changed"])
        self.assertTrue(probe.observe_summary(
            baseline, frames, threshold=3.0)["changed"])


class VerdictTests(unittest.TestCase):
    """v3.1 结论：无前台对照，后台自校验回路是唯一裁决者，alive 仅参考。"""

    @staticmethod
    def _state(alive, max_diff=50.0):
        return {"frames": 3, "mean_diffs": [max_diff, max_diff],
                "max_diff": max_diff, "alive": alive}

    def _report(self, *, pw_black=False, rendering=None, tickle=None, bg=None):
        return {
            "captures": {
                "printwindow": {"ok": not pw_black, "black": pw_black},
                "rendering": rendering or {},
            },
            "tickle_test": tickle or {},
            "click": bg or {},
            "drag": None,
        }

    @staticmethod
    def _bg_round(open_ok, close_ok, esc=None):
        return {"open": {"ok": open_ok, "observe": {}},
                "close": {"ok": close_ok, "via": "yellow_button"},
                "keyboard_esc_ok": esc}

    def test_channel_verified_is_feasible(self):
        report = self._report(
            rendering={"occluded": self._state(False, 0.05)},  # alive 只是参考
            bg={"post": self._bg_round(True, True, esc=False)})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "可行")
        self.assertEqual(verdict["best_transport"], "post")
        self.assertIn("自校验通过", verdict["capture_verdict"])
        self.assertIn("未生效", verdict["keyboard_verdict"])

    def test_alive_is_informational_only(self):
        # 渲染全灭（旧阈值口径）不再挡「可行」——行为证据优先
        report = self._report(
            rendering={"foreground": self._state(False, 0.05),
                       "occluded": self._state(False, 0.05)},
            bg={"post": self._bg_round(True, True)})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "可行")
        self.assertIn("低于参考阈值", verdict["rendering_note"])

    def test_tickle_only_is_conditional(self):
        report = self._report(
            tickle={"changed": True, "region_max_diff": 5.0})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "有条件可行")
        self.assertIn("hover", verdict["capture_verdict"])
        self.assertIn("残留菜单", verdict["background_verdict"])

    def test_black_baseline_is_infeasible(self):
        report = self._report(pw_black=True,
                              bg={"post": self._bg_round(True, True)})
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "不可行")
        self.assertIn("基线", verdict["capture_verdict"])

    def test_no_signal_at_all_needs_manual_review(self):
        # 无前台对照后，自校验与 hover 全灭无法区分「输入不消费」与
        # 「状态异常」——结论必须提示人工确认游戏画面
        report = self._report()
        verdict = probe.compute_verdict(report)
        self.assertEqual(verdict["overall"], "不可行")
        self.assertIn("人工确认", verdict["overall_note"])
        self.assertIn("人工揭开", verdict["background_verdict"])

    def test_keyboard_verdicts(self):
        esc_ok = probe.compute_verdict(
            self._report(bg={"post": self._bg_round(True, True, esc=True)}))
        self.assertIn("可用", esc_ok["keyboard_verdict"])
        esc_fail = probe.compute_verdict(
            self._report(bg={"post": self._bg_round(True, True, esc=False)}))
        self.assertIn("未生效", esc_fail["keyboard_verdict"])
        none_tested = probe.compute_verdict(self._report())
        self.assertIn("未测得", none_tested["keyboard_verdict"])

    def test_background_prefers_post_then_send_then_flick(self):
        report = self._report(
            bg={"send": self._bg_round(True, True),
                "post_flick": self._bg_round(True, True)})
        self.assertEqual(probe.compute_verdict(report)["best_transport"],
                         "send")


if __name__ == "__main__":
    unittest.main()
