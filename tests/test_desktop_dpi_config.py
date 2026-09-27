# -*- coding: utf-8 -*-
"""desktop_dpi 自动解析：显式传参 > ui_config 覆盖 > layout 检测。

进程声明 DPI 感知后 desktop_dpi 是“捕获帧一致性校验基准”，
不再要求 96：125% 缩放 → 120，帧校验按真实值比对。
"""

import os
import unittest
from unittest import mock

import config
from src.recommendation_config import RecommendationConfig


class DesktopDpiResolutionTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "非 Windows 平台验证回退值")
    def test_default_resolves_to_reference_on_non_windows(self):
        self.assertEqual(96, RecommendationConfig().desktop_dpi)

    def test_explicit_value_is_respected(self):
        self.assertEqual(120, RecommendationConfig(desktop_dpi=120).desktop_dpi)

    def test_ui_config_override_is_applied(self):
        with mock.patch.object(config, "_UI", {"desktop_dpi": 144}):
            self.assertEqual(144, RecommendationConfig().desktop_dpi)

    def test_ui_config_override_loses_to_explicit_argument(self):
        with mock.patch.object(config, "_UI", {"desktop_dpi": 144}):
            self.assertEqual(96, RecommendationConfig(desktop_dpi=96).desktop_dpi)


if __name__ == "__main__":
    unittest.main()
