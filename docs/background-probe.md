# 后台模式可行性探针 v2 —— Windows 机器操作手册

> 目标：回答三个只有实机才能回答的问题
> 1. **渲染**：炉石在【前台 / 可见但失焦 / 被遮挡 / 遮挡+幽灵透明】四种状态下画面还在动吗？
> 2. **输入**：炉石（Unity）消费 PostMessage/SendMessage 的合成鼠标消息吗？落点采纳消息坐标还是真实光标位置？（先在前台做对照，再在遮挡下测）
> 3. **键盘/拖拽**：ESC 与拖拽的消息序列能否生效？
>
> 探针全程只做无风险动作（大厅开/关选项菜单），不进对局、不改游戏设置（拖牌探针在练习模式）。

## 双机流程

```
本机(开发)                    Windows 机器(有炉石)
git push feat/background-mode ──► git pull
                               uv run --no-project python probe_background.py
回传 probe_report.json ◄──────  按提示操作约 8~10 分钟
分析报告 → 决定是否实现后台后端
```

## 一、准备（Windows 机器，只需做一次）

1. **uv**（家里电脑已装就跳过）：`winget install astral-sh.uv`
2. 拉取分支：
   ```powershell
   git fetch origin
   git checkout feat/background-mode
   ```
3. 用 uv 建 Python 3.12 环境（本机没有 3.12 时 uv 会自动下载）：
   ```powershell
   uv venv --python 3.12
   ```
   探针只依赖三个包，按需安装即可（比全量 requirements 快得多）：
   ```powershell
   uv pip install numpy Pillow pywin32
   ```
   想直接复用主项目全量环境也行：`uv pip install -r requirements.txt`
   （paddle/paddleocr 很大，探针用不到，非必需）。
4. **以管理员身份**打开 PowerShell，进入项目目录，用 `.venv` 里的解释器运行：
   ```powershell
   .venv\Scripts\Activate.ps1          # 激活后直接 python probe_background.py
   # 或免激活等价写法：
   uv run --no-project python probe_background.py
   ```

> 为什么要管理员：若炉石以更高权限运行，普通权限进程发的消息会被系统静默拦截。探针会自检并在报告里记录。

## 二、术语与总原则

- **大厅（主菜单）**：登录战网进入游戏后，有「对战模式 / 竞技场 / 酒馆战棋」大按钮、右下角有齿轮图标的界面。探针所有点击都假设停在这个界面；不确定就看 `probe_out/state_check.png`。
- 每个「按回车」的提示都是**流程节点**：按提示先把桌面摆好（谁在前台、谁盖住谁），再回到 cmd 窗口按回车，然后**手离开鼠标**等探针自动采样。
- 全程不动游戏设置；R4 会把炉石窗口临时设为近透明（ghost-alpha），测完自动还原。

## 三、运行步骤（约 8~10 分钟）

```powershell
uv run --no-project python probe_background.py
```

| 阶段 | 你要做的 | 它在测什么 |
|---|---|---|
| 0. 窗口信息 | 登录炉石停在大厅 → 回车；确认 `state_check.png` 截图是大厅 (y) | 环境登记 + 画面状态对齐 |
| R1 前台 | 回车后 3 秒内点一下炉石**顶部中间空白**把它切前台，手离开鼠标 | 前台渲染（活帧基准） |
| R2 可见失焦 | 点 cmd 激活它但**不遮住**炉石 → 回车 | 失焦是否停渲染 |
| R3 完全遮挡 | cmd **完全盖住**炉石 → 回车 | 遮挡是否停渲染（v1 的 alive=False 之谜在这解开） |
| R4 遮挡+幽灵透明 | 自动执行（窗口临时 alpha=3，随后还原） | 经典保活手段能否绕过遮挡停渲染 |
| F1 前台输入对照 | 炉石前台、光标停在炉石窗口**外** → 回车，盯着炉石看是否弹出菜单，回 cmd 答 y/n | **合成消息前台是否被消费**（判死刑还是判可救） |
| 后台点击 A/B/C | 每轮前：确认大厅 → cmd 盖住炉石 → 回车，手不动 | 遮挡下三种注入通道是否生效 |
| （可选）拖牌 | `--drag`，按提示进练习模式 | 拖拽消息序列 |

常用参数：

```powershell
uv run --no-project python probe_background.py --strategies post,post_flick  # 只测指定后台策略
uv run --no-project python probe_background.py --drag                        # 追加拖牌探针
```

## 四、结果判读（报告结尾有自动结论，对照下表理解）

| 观察 | 含义 |
|---|---|
| R3 `alive=True` | 遮挡下照常渲染 → 后台截图无障碍 |
| R3 `False` 但 R4 `True` | 遮挡会暂停渲染，但 ghost-alpha 能救 → 后台方案需配合窗口透明 |
| R1 `True`、R2 `False` | 失焦即停渲染（RunInBackground 关闭）→ 后台截图基本无望，先看 R4 |
| F1 `post` 生效 | **关键转折**：合成消息炉石认 → 后台轮失败只是遮挡/失焦问题，值得攻关 |
| F1 全部失败 | 炉石根本不消费合成鼠标消息 → 后台输入判死刑，只做窗口化支持 |
| 后台轮 `full_diff` 有变化但 `center_diff` 没有 | 点击生效但落点跟着真实光标走 → 走瞬移方案（C / WithCursorPos） |
| `esc=true` | 后台键盘也可用 |
| 拖牌 | 看 `drag_before/after.png` 人工确认随从是否放下 |

## 五、结果回传（二选一）

**方式 1（推荐，快）**：把 `probe_report.json` 的完整内容直接粘贴回对话，再挑关键截图
（`state_check.png`、`render_occluded.png`、`fg_post_after.png`、`click_post_baseline.png`、`click_post_menu_open.png`）发出来。

**方式 2（走 git）**：
```powershell
git add probe_out
git commit -m "chore(probe): 回传后台可行性探针报告(v2)"
git push origin feat/background-mode
```
然后在本机对话里说一声"已 push"，开发侧拉下来分析。

## 六、已知边界与风险

- **最小化窗口**：不在支持范围（PrintWindow 只能拿到陈旧帧），探针检测到会直接退出。
- **独占全屏**：与窗口截图天然不兼容；首轮实测 PrintWindow 非黑（你的 2048×1152 应是无边框/全屏窗口），若重跑出现黑帧请切「窗口化」。
- **炉石盒子（HSAng）**：其推荐面板走桌面 OCR，后台模式下被盖住会识别失败——后台后端实现时另行处理，与本探针无关。
- **封号风险提示**：探针与现有脚本同级别（API 注入，不碰进程/内存），但自动化行为本身始终有被检测的风险，自行斟酌。

elo psy congroo
