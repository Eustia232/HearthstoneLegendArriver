from time import sleep

# 声明进程 DPI 感知：必须在任何窗口/截屏之前（非 Windows 无害 no-op）。
import layout
layout.enable_dpi_awareness()

from FSM_action import system_exit, AutoHS_automata
import keyboard
from print_info import print_info_init
from FSM_action import init

import constants.constants


if __name__ == "__main__":
    print("分辨率：任意分辨率均可（推荐 16:9，如 1920x1080 / 2560x1440 / 3840x2160），"
          "坐标会自动按分辨率换算")
    print("Windows 缩放任意（推荐 100%）；炉石内游戏分辨率需与桌面一致并使用全屏")
    print("请保持炉石传说与炉石盒子可见，程序将自动读取左侧 AI 打法并执行")
    print("按 Ctrl+Q 可随时停止自动化并退出程序")
    
    MY_NAME = constants.constants.YOUR_NAME
    print("你好"+MY_NAME)
    sleep(2)
    print_info_init()
    init()
    keyboard.add_hotkey("ctrl+q", system_exit)
    try:
        AutoHS_automata()
    except KeyboardInterrupt:
        # Ctrl+Q interrupts the main thread even while it is blocked in input().
        pass

