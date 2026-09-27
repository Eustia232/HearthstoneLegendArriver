# -*- coding: utf-8 -*-
"""get_state 判定规则与 layout 登记表的一致性（依赖 win32，仅 Windows 可跑）。"""

import os
import unittest

import layout

@unittest.skipUnless(os.name == "nt", "get_screen 依赖 win32，仅 Windows 可跑")
class GetStateProbeRuleTests(unittest.TestCase):
    def test_rules_only_reference_registered_probe_keys(self):
        import get_screen

        registered = {p["key"] for p in layout.STATE_PROBE_POINTS}
        for _state, probe_key, _expected in get_screen._STATE_PROBE_RULES:
            self.assertIn(probe_key, registered)

    def test_rules_keep_the_original_judging_order(self):
        """判定顺序必须与旧版一致：主界面 → 选英雄 → 匹配 → 选牌。"""
        import get_screen
        from constants.constants import (
            FSM_MAIN_MENU, FSM_CHOOSING_HERO, FSM_MATCHING, FSM_CHOOSING_CARD,
        )

        states = [state for state, _key, _rgb in get_screen._STATE_PROBE_RULES]
        self.assertEqual(
            [FSM_MAIN_MENU, FSM_MAIN_MENU,
             FSM_CHOOSING_HERO, FSM_MATCHING, FSM_CHOOSING_CARD],
            states)

    def test_without_hearthstone_the_state_is_leave(self):
        import get_screen
        from constants.constants import FSM_LEAVE_HS

        self.assertEqual(FSM_LEAVE_HS, get_screen.get_state())


if __name__ == "__main__":
    unittest.main()
