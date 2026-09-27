# 后台模式可行性探针 v3.1 —— Windows 机器操作手册（纯后台版）

> v3 解决的核心问题：**PrintWindow 可能返回陈旧帧**，单帧状态确认会被骗
> （v2 的教训：残留选项菜单 + 陈旧大厅帧 → 齿轮被挡 → 后台点击全部"假失败"）。
> v3 把「确认状态、验证通道、容忍呈现延迟」做成**自校验回路**：
> 开菜单和关菜单两个方向都要被多帧截图证实，通道才算健康。
>
> **v3.1 变更**：去掉前台对照（前台已两次实测通过、项目前台模式已适配，且 F1
> 是唯一会往游戏里点东西的环节——上轮实证它把别的界面点开了，正是状态污染源）；
> 渲染矩阵改 `--render` 可选。后台自校验回路是唯一裁决者。

## 已确认的实测结论（v1/v2 + 上轮数据，无需重测）

1. **渲染不是瓶颈**：前台/失焦/遮挡/幽灵透明的 idle 帧差同为 0.01~0.08，菜单打开 = 27~54 —— 游戏在所有状态下都在渲染。
2. **前台合成消息被消费**（中央差异 27.4 / 61.9，两次实测）。
3. **键盘（ESC）不被消费**（Unity Raw Input）—— 本项目纯鼠标操作，无影响。
4. **陈旧帧欺骗的实证**：上轮 F1 差异 61.9 实际是「点到其他界面」，state_check 却显示大厅——单帧确认不可信，一切状态判定必须走自校验回路。
5. 待验证：后台消息稳定性（自校验）、呈现延迟量级（observe）、hover 唤醒（tickle）、**拖拽**（一直没测）。

## 双机流程

```
本机(开发)                    Windows 机器(有炉石)
git push feat/background-mode ──► git pull
                               uv run --no-project python probe_background.py --drag
回传 probe_report.json ◄──────  按提示操作约 10 分钟
分析报告 → 决定是否实现后台后端
```

## 一、准备（Windows 机器，只需做一次）

1. **uv**（已装跳过）：`winget install astral-sh.uv`
2. 拉取分支：
   ```powershell
   git fetch origin
   git checkout feat/background-mode
   git pull
   ```
3. 环境（探针只依赖三个包）：
   ```powershell
   uv venv --python 3.12      # 已建过就跳过
   uv pip install numpy Pillow pywin32
   ```
4. **以管理员身份**打开 PowerShell，进项目目录，用 `.venv` 运行：
   ```powershell
   uv run --no-project python probe_background.py
   ```

## 二、重跑前必做（重要）

1. **揭开炉石，肉眼确认：停在大厅、没有残留的菜单/面板**。上一轮 F1 把「其他界面」
   点开了；v2 也可能卡着选项菜单——都手动关掉，亲眼看到大厅再继续。
2. 确认后把 cmd 重新摆好，再运行探针。

## 三、运行步骤（约 5 分钟）

```powershell
uv run --no-project python probe_background.py --drag
```

| 阶段 | 你要做的 | 它在测什么 |
|---|---|---|
| 0. 窗口信息 + 大厅确认 | 登录炉石停在大厅 → 回车 → 看 `state_check.png` 答 y | 环境登记 + 粗确认 |
| hover-tickle | cmd 盖住炉石 → 回车，手不动 | 只发悬停不点击：输入被消费 + 画面随输入刷新？ |
| 自校验轮 A/B/C | 每轮自动：清扫残留菜单 → 点齿轮开 → 多帧确认 → 黄按钮关 → 多帧确认 → 重开测 ESC | **后台消息的权威判定**（开/关双向作证） |
| 拖牌 `--drag` | 进练习模式，手牌留一张随从牌 → 回车 | 拖拽消息序列（后端最后一问） |
| 收尾 | 揭开炉石看一眼有没有残留菜单 | 防止残留状态骗过下一次 |

（渲染四状态矩阵默认跳过——两轮实测已证明四状态均在渲染；需要复测加 `--render`。）

关键改进：每次动作后采样 **5 帧 × 0.6s**，逐帧差异直接量出「呈现延迟」；
「开/关」双向都有真实状态变化作证后，陈旧帧无法再欺骗判定。

## 四、结果判读

| 观察 | 含义 |
|---|---|
| 某策略 `open=True close=True` | **后台通道自校验通过** → 可行，进入后端实现 |
| `open` 的 `changed_at=2` | 呈现延迟 ≈1 个采样间隔（0.6s）→ 后端动作后等 1s 再截图即可 |
| hover-tickle `changed=True` | 后端可在每次截图前用无害 hover 唤醒画面（可靠截图机制） |
| hover 有响应但自校验不过 | 输入通道活着，状态机受扰（残留菜单/面板）→ 清理后重跑 |
| 自校验 + hover 全失败 | 先人工揭开炉石确认画面状态；重跑仍全灭则后台输入判死，降级窗口化前台方案 |
| 拖牌 | 看 `drag_before/after.png` 人工确认随从真的放下 |

## 五、结果回传

```powershell
git add probe_out
git commit -m "chore(probe): 回传 v3 报告"
git push origin feat/background-mode
```

（v3 起 `.gitignore` 已放行 `probe_out/*.json`，`git add probe_out` 就能带上
`probe_report.json`；如果还是没有，用 `git add -f probe_out/probe_report.json`。）
推送后在本机对话里说一声"已 push"。

## 六、已知边界与风险

- **最小化窗口**：不在支持范围，探针检测到会退出。
- **炉石盒子（HSAng）**：推荐面板走桌面 OCR，后台模式下被盖住会识别失败——后端实现时另行处理。
- **封号风险提示**：探针与现有脚本同级别（API 注入，不碰进程/内存），自动化行为本身始终有被检测的风险，自行斟酌。

elo psy congroo
