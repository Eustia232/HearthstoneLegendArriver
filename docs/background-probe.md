# 后台模式可行性探针 —— Windows 机器操作手册

> 目标：回答两个只有实机才能回答的问题
> 1. 炉石窗口**被遮挡**时，能否拿到「活帧」截图（PrintWindow / 窗口 DC / 桌面 BitBlt 三种方式对比）？
> 2. 炉石（Unity）是否消费 PostMessage / SendMessage 的**合成鼠标消息**？点击落点是消息坐标还是真实光标位置？键盘（ESC）呢？拖拽呢？
>
> 全程只做无风险动作（主菜单开/关选项菜单），**不进对局、不改任何游戏设置**（拖牌探针除外，且在练习模式）。

## 双机流程

```
本机(开发)                    Windows 机器(有炉石)
git push feat/background-mode ──► git pull
                               python probe_background.py
回传 probe_report.json ◄──────  按提示操作几分钟
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

## 二、运行

```powershell
python probe_background.py
```

然后跟着屏幕提示走，全程约 3~5 分钟：

| 步骤 | 你要做的 | 它在测什么 |
|---|---|---|
| 1. 窗口信息 | 无（自动打印窗口矩形/客户区/显示模式/管理员/DPI） | 环境登记 |
| 2. 截图探针 | 提示时**用任意窗口完全盖住炉石**，回车 | 遮挡下三种截图方式能否拿到活帧（间隔 1s 两帧对比，主菜单有持续动画） |
| 3. 点击策略 A（纯 PostMessage） | **手不要碰鼠标**，光标停在屏幕角落 | 点击是否生效、落点是否采纳消息坐标 |
| 4. 点击策略 B（SendMessage） | 同上 | 同步注入对照 |
| 5. 点击策略 C（PostMessage+光标瞬移） | 操作瞬间真实光标会闪一下，属正常 | 游戏读真实光标时的兜底方案 |
| 6. （可选）拖牌探针 | `python probe_background.py --drag`，按提示进练习模式 | 消息序列能否完成"按下→拖动→松开" |

每轮点击探针的动作：点主菜单右下角**设置齿轮**打开选项菜单 → 点黄色**"完成"**按钮关闭 → 再开一次 → 按 **ESC** 关闭。全部由前后截图差异自动判定；万一菜单卡住，探针会提示你手动关掉再回车。

### 常用参数

```powershell
python probe_background.py --strategies A,C   # 只测指定策略
python probe_background.py --drag             # 追加拖牌探针
python probe_background.py --out probe_out2   # 指定输出目录
```

## 三、结果判读

报告在 `probe_out/probe_report.json`，结尾有「探针结论」摘要。对照表：

| 探针结果 | 含义 | 下一步 |
|---|---|---|
| PrintWindow 遮挡下 `alive=true` 且非黑 | 后台截图可行 | ✅ |
| PrintWindow 全黑（`black=true`） | 独占全屏模式下常见 | 把炉石切到**「窗口化」**重跑；后台模式本来就需要窗口化 |
| 策略 A 开/关菜单都成功 | **最优**：纯消息注入即可后台，无感 | 直接进入后台后端实现 |
| A 失败、C 成功 | 游戏读真实光标位置 | 后台可做，但每次操作光标会闪（MaaNTE 同款方案） |
| A/B/C 全失败 | 炉石不吃合成消息 | 后台路线搁置，只做窗口化支持 |
| `esc=true` | 后台键盘也可用 | 锦上添花 |
| 拖牌探针 | 看 `drag_before/after.png` 人工确认随从是否真的放下 | 拖拽是后台化的关键风险点 |

## 四、结果回传（二选一）

**方式 1（推荐，快）**：把 `probe_report.json` 的完整内容直接粘贴回对话，再挑几张关键截图
（`capture_printwindow_1.png`、`click_post_baseline.png`、`click_post_menu_open.png`）发出来。

**方式 2（走 git）**：
```powershell
git add probe_out
git commit -m "chore(probe): 回传后台可行性探针报告"
git push origin feat/background-mode
```
然后在本机对话里说一声"已 push"，开发侧拉下来分析。

## 五、已知边界与风险

- **最小化窗口**：不在支持范围（PrintWindow 只能拿到最小化前的陈旧帧），探针检测到最小化会直接退出。
- **独占全屏**：与窗口截图天然不兼容；请用「窗口化」（无边框也可以试）。
- **炉石盒子（HSAng）**：其推荐面板走桌面 OCR，后台模式下被其他窗口盖住会识别失败——这是后台后端实现时要另行处理的限制，与本探针无关。
- **封号风险提示**：探针与现有脚本同级别（API 注入，不碰进程/内存），但自动化行为本身始终有被检测的风险，自行斟酌。

elo psy congroo
