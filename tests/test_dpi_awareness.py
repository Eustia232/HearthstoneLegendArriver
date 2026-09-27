# -*- coding: utf-8 -*-
"""DPI 感知声明：物理像素工作模式（Windows 专属路径 + 非 Windows 兜底）。"""

import os
import unittest
from unittest import mock

import layout


class NonWindowsTests(unittest.TestCase):
    @unittest.skipUnless(os.name != "nt", "仅非 Windows 平台验证兜底")
    def test_non_windows_returns_false_without_raising(self):
        self.assertFalse(layout.enable_dpi_awareness())


@unittest.skipUnless(os.name == "nt", "ctypes.windll 仅 Windows 可用")
class WindowsTests(unittest.TestCase):
    def test_shcore_success_returns_true(self):
        import ctypes

        with mock.patch.object(ctypes.windll.shcore, "SetProcessDpiAwareness",
                               create=True, return_value=0) as declare:
            self.assertTrue(layout.enable_dpi_awareness())
            declare.assert_called_once_with(2)

    def test_shcore_failure_falls_back_to_user32(self):
        import ctypes

        with mock.patch.object(ctypes.windll.shcore, "SetProcessDpiAwareness",
                               create=True, side_effect=OSError("no shcore")), \
             mock.patch.object(ctypes.windll.user32, "SetProcessDPIAware",
                               create=True, return_value=1):
            self.assertTrue(layout.enable_dpi_awareness())

    def test_both_failing_returns_false(self):
        import ctypes

        with mock.patch.object(ctypes.windll.shcore, "SetProcessDpiAwareness",
                               create=True, return_value=1), \
             mock.patch.object(ctypes.windll.user32, "SetProcessDPIAware",
                               create=True, return_value=0):
            self.assertFalse(layout.enable_dpi_awareness())


if __name__ == "__main__":
    unittest.main()
