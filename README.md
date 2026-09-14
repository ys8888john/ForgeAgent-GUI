# ForgeAgent-GUI

面向 [ACP](https://agentclientprotocol.com)（Agent Client Protocol）agent 的桌面客户端，
用来跟 `agentd` 对话。两种形态：终端里的 TUI，和**独立窗口的 GUI**。

**它不是 agent 的一部分。** 它通过 stdio 把 agentd 当子进程拉起来，用 JSON-RPC 通信——
跟 Zed、JetBrains 那些客户端是同一个姿势。所以换 UI 不用动内核，换内核不用动 UI。

## 形态概览

- **TUI**（`forgeagent`）：终端字符界面，依赖 textual。
- **GUI**：界面本体是同一份 `gui/assets/index.html`（自包含 HTML/CSS/JS，三平台共用），
  由下面两种**载体**渲染——同一份 HTML，只是"壳"不同：

  | 载体 | 启动 | 渲染 | 依赖 | 定位 |
  |---|---|---|---|---|
  | `electron`（默认）| `forgeagent-gui` | **自带 Chromium**（Electron 壳）| Node + `npm i` 装 electron | 主前端，最像 WorkBuddy |
  | `serve` | `forgeagent-gui --mode serve` | 不开窗，只起服务 | 无 | 交给外部（浏览器 / IDE）渲染 |

两种载体 + TUI 共用同一套协议层（`acp_client.py`）和同一个后端（HTTP），功能等价。
换壳不改协议、内核、存储任何一行。

**为什么主前端是 Electron 而不是系统 WebView**：系统 WebView（Windows 上的 Edge WebView2）
本质也是 Chromium。在显卡驱动 / Hyper-V 有问题的机器上，WebView2 的
**浏览器进程会直接崩**（报 `The instance of CoreWebView2 is no longer valid
because the browser process crashed`），症状是窗口能开、页面能加载、JS 跑两下就再没动静。
而 Electron 自带一套 Chromium + 软件渲染兜底（swiftshader），对同样的 GPU 问题钝感得多——
**WorkBuddy 自己就是 Electron 应用，能在崩机王上跑稳，也是同一套道理**。所以我们
参考 WorkBuddy，把前端定为「Electron 壳包 HTML」的形态。

Electron 壳在 `electron/`：先 `npm install` 装好 electron，再 `forgeagent-gui` 即可。
详见下文「跑起来」。

### 前端 ↔ Python 走本机 HTTP，不走 pywebview 的 js_api

GUI 这一版的界面和 Python 之间是这样通信的：

```
HTML 窗口（Electron 壳）──HTTP（只绑 127.0.0.1）──> UiServer ──> Bridge ──> AcpClient ──stdio──> agentd
```

`UiServer` 只监听 `127.0.0.1`，端口由内核随机分配，每个进程一次性 token
（通过 URL fragment 和页面内联两种方式交给前端），每个请求校验
`X-ForgeAgent-Token`。**它不是"把 ACP 搬上 web"**：ACP 本身仍然只在 stdio 上跑，
agentd 一个字没改；这一跳纯属「本机窗口 ↔ 本机 Python 进程」。

早期试过让界面和 Python 走 pywebview 的 `js_api`，有两个坑（脚本注入时机、后端死锁），
所以改成本机 HTTP：

换成 HTTP 之后：不依赖脚本注入，三平台行为一致，Python 侧不需要 `evaluate_js`，
而且**能脱离 GUI 直接测**（`tests/test_gui_server.py` 真发 HTTP 请求跑完整一轮）。

哪天真要做浏览器版，SDK 已经带了服务端实现（`acp.http.asgi` 的 `create_asgi_app`、
`acp.ws.server`），挂个 uvicorn 即可 —— 但那是另一个话题。

## 跑起来

### Windows

**别用裸 `python`** —— 如果系统装过微软商店的 Python 占位别名（设置 → 应用 →
高级应用设置 → 应用执行别名），`python` 会解析到 `WindowsApps\python.exe`，
报 *"Python was not found; run without arguments to install from the Microsoft Store"*。
这台机器上就是这种情况。用 venv 里的解释器，绕开 PATH 上的别名：

```powershell
cd D:\workspace\ForgeAgent-GUI

# 第一次：建 venv 并装依赖（agentd 是另一个仓库，一起装进来）
<你的 Python> -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -e "D:\workspace\Agentd"

# 默认前端是 Electron（自带 Chromium，最像 WorkBuddy）。electron 是 Node 包，单独装一次：
cd electron
npm install
cd ..

# GUI（独立窗口，默认 Electron 形态）
.\.venv\Scripts\python.exe -m forgeagent.gui

# TUI（终端界面）
.\.venv\Scripts\python.exe -m forgeagent

# 三跳验证
.\.venv\Scripts\python.exe scripts\e2e.py --cwd D:\workspace\Agentd
```

想少敲路径就激活 venv（若 PowerShell 报执行策略错误，用上面带全路径的写法即可）：

```powershell
.\.venv\Scripts\Activate.ps1
forgeagent-gui
```

### Linux / macOS

同一套流程，venv 的可执行文件在 `bin/` 而不是 `Scripts/`：

```bash
cd ~/workspace/ForgeAgent-GUI
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]" -e "$HOME/workspace/Agentd"

./.venv/bin/python -m forgeagent.gui      # GUI
./.venv/bin/python -m forgeagent          # TUI
./.venv/bin/python scripts/e2e.py --cwd "$HOME/workspace/Agentd"
```

### 跨平台的系统依赖

- **TUI**：纯 pip 依赖（textual），三平台都一样，不需要装系统包。
- **electron 前端（默认）**：需要 Node.js（含 npm）。一次 `cd electron && npm install`
  会把 Electron 及其自带 Chromium 装进 `electron/node_modules`，之后 `forgeagent-gui`
  直接调用，**不依赖系统 WebView**。Chromium 是 Electron 自带、带软件渲染兜底，
  所以哪怕系统 WebView2 崩机王也能跑（同 WorkBuddy）。

其余跨平台注意事项：
- 启动 agent 的默认命令是 `[sys.executable, "-m", "agentd.server"]`，不走 shell，
  不依赖 PATH 上的 `python`，所以三平台行为一致。
- 从环境变量 `FORGEAGENT_AGENT_CMD` 覆盖时，`shlex` 的 posix 模式按 `os.sep` 自动切换，
  避免 Windows 路径里的反斜杠被当成转义符。

### 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `FORGEAGENT_AGENT_CMD` | 拉起 agent 的命令 | `sys.executable -m agentd.server` |
| `FORGEAGENT_CWD` | agent 的工作目录 | 当前目录 |
| `AGENTD_LLM_BACKEND` | 透传给 agentd：`fake` / `ollama` / `openai_compat` | `ollama` |
| `AGENTD_OLLAMA_MODEL` | 透传给 agentd | `auto`（见下） |
| `AGENTD_OLLAMA_HOST` | 透传给 agentd | `http://localhost:11434` |
| `FORGEAGENT_GUI_MODE` | 前端载体：`electron` / `serve` | `electron` |
| `FORGEAGENT_PYTHON` | electron 模式下拉起 Python 后端的解释器（由 `forgeagent-gui` 自动设为 `sys.executable`）| 系统 `python3` |
| `FORGEAGENT_UI_DEBUG` | 设为 1 时把本机服务的每个请求打到 stderr | 关 |

**默认用 `sys.executable` 而不是 `"python"` 拉起 agentd。** 两者装在同一
个 venv 时，PATH 上的 `python` 未必就是那一个；写成 `"python"` 的话症状是
"agent 起不来"，而报错停在 `ModuleNotFoundError`，很难联想到是解释器选错了。

**`AGENTD_OLLAMA_MODEL` 默认 `auto`**：首次调用时去 `/api/tags` 问本机有什么
模型再挑一个。默认值一度写死成 `qwen3`，但这台机器上装的是 `qwen3.5:9b-text`，
Ollama 返回 404，而 404 的表现是**回复一片空白**——界面上干干净净，什么错都没有。
改成 auto 之后换台机器也能开箱即通。

不连真模型先验链路：

```bash
AGENTD_LLM_BACKEND=fake forgeagent
```

接 Ollama（什么都不用配，auto 会自己找模型）：

```bash
forgeagent
```

### 三跳验证

前端 → 后端 → Ollama，任一节断掉症状都长得差不多（"没反应"或"一片空白"），
光看界面分不清是哪一跳坏了。用这个脚本一跳一跳验：

```bash
python scripts/e2e.py                 # 三跳全验
python scripts/e2e.py --model qwen3   # 指定模型（复现 404 场景）
python scripts/e2e.py --skip-ui       # 只验后端两跳
```

退出码 0 表示全通。坏在哪跳会直接指出来，比如：

```
第 2 跳：agentd（ACP over stdio）
  ✅ 握手成功，会话 sess_1900559c3b754877
  ❌ agentd 报错：LLMError: Ollama HTTP 404：{"error":"model 'qwen3' not found"}
```

### 按键

| 键 | 作用 |
|---|---|
| 回车 | 发送 |
| `Ctrl+C` | 退出（会带走 agent 子进程） |
| `Ctrl+L` | 切换 agent 日志面板 |

## 代码结构

```
forgeagent/
  acp_client.py     协议层：拉起子进程 + JSON-RPC + 事件折叠。只依赖标准库
  app.py            TUI 层：Textual 界面（需要 Textual）
  __main__.py       TUI 入口
  gui/
    bridge.py       桥接层：纯 Python，不依赖任何 GUI 库，可脱离 GUI 单测
    server.py       本机 UI 服务：bridge 的 HTTP 封装 + 静态页面（含 token 校验）
    assets/         HTML 前端（HTML/CSS/JS，无外部依赖、离线可用）
    __main__.py     GUI 入口（--mode 选前端载体）
electron/          Electron 壳（main.js + package.json），默认前端载体：
                  自带 Chromium 渲染 assets/index.html，参考 WorkBuddy 的前端形态
scripts/
  e2e.py            三跳验证（Ollama / agentd / TUI 界面）
tests/
  test_acp_client.py   reducer 单测，不用起进程
  test_app.py          TUI 冒烟（headless）
  test_gui_bridge.py   桥接层单测（注入假 client，无需图形环境）
  test_gui_server.py   本机 UI 服务单测：真起 HTTP 服务，真发请求
  test_end_to_end.py   真起子进程跑一遍完整握手 + 流式
```

**为什么要分这么多层：** 窗口必须有图形环境才能跑，CI 里测不了；但只要把界面
和 Python 之间的通信做成 HTTP（`server.py`），这一跳就能脱离 GUI 测了 ——
`test_gui_server.py` 真起服务、真发请求，把「前端那一跳」验得干干净净。
再往下一层，`bridge.py` 里一行 GUI 代码都没有，塞个假的 async client 就能测全部逻辑。

### 三个不显然的设计决定

**1. 手写 JSON-RPC，不用 SDK 的高层 helper。**
官方 SDK 的 `spawn_agent_process` 签名跨版本变过（factory 风格 vs 直接传实例），靠不住。
这里跟 agentd 的 `tests/test_acp.py` 用同一套约定。

**2. stderr 必须捕获。**
agentd 的日志全走 stderr（因为 stdout 要保持纯 JSON-RPC）。Textual 一旦接管终端，
这些日志直接打出来会把界面撕碎。所以走管道存进环形缓冲，`Ctrl+L` 才看。

**3. 事件流先 reduce 成 `Turn`，UI 只渲染状态。**
一句话会被拆成几十个 chunk 陆续到达，工具调用还有 start/update/done 三态。
拿流直接渲染必然重复、乱序、闪烁。这个思路是从 Panda 的 README 学来的。

**4. 所有 Static 都关掉了 markup。**
Textual 的 `Static` 默认 `markup=True`，会把 `[xxx]` 当样式标签解析。
LLM 输出里方括号太常见了（`[DONE]`、数组、Markdown 链接），实测 `[DONE]`
会被**整段吞掉**——不报错、不留痕，界面上直接消失，比崩溃还难查。
所以这里统一构造 `rich.text.Text` 手动上色，不碰 markup。

**5. 错误走 thought 通道，前端认 `[错误]` 前缀。**
ACP 的 `stop_reason` 只有 `end_turn / max_tokens / max_turn_requests /
refusal / cancelled` 五种，**没有 error**。agentd 只好把错误塞进 thought 通道
并打上 `[错误]` 前缀（`acp_stdio.py` 的 `_STOP_REASON_MAP` 有说明）。
前端在 reducer 里识别这个前缀，剥掉后单独用红色渲染，而不是混在"思考"里
让人以为模型在自言自语。两个仓库各自定义了同一个字面量，改一处要改另一处——
最坏情况只是错误退化成普通思考文本，不会崩。

## 当前限制

- **只有文本流。** 工具调用卡片、diff、权限弹窗都没做——不是 UI 写不了，
  是 agentd 目前根本不产出这些事件（`acp_stdio.py` 里明确写着"工具调用类事件还没映射"）。
  内核补齐后，挂卡片的位置在 `acp_client.py` 的 `_apply_update` 里，已留好分支。
- HTML 前端里的 Markdown 是**自带的极简实现**（标题、粗斜体、列表、行内代码、围栏代码块），
  没引外部库 —— 离线也能用，代价是高亮、表格这些还没做。
- **会话侧栏 / 续聊已可用**（参考 WorkBuddy 的会话侧栏）：
  左侧栏列出 `~/.agentd/sessions.db` 里的历史会话（标题、最近时间、条数、末条预览），
  点一下即「续聊」。agentd 内核在每轮 `handle()` 开头会把整段历史 load 进上下文
  （`agentd/kernel/kernel.py:95`），所以 GUI 只要复用旧 `sessionId`、不去调 `session/new`，
  LLM 自然就接着上次聊；「+ 新对话」则走 ACP `session/new` 开干净会话。
  GUI 只读那一份 SQLite 库（另开 read-only 连接，WAL 并发读不冲突），不碰写方。
  后端接口：`GET /api/sessions`、`GET /api/session/<id>`、`POST /api/session/resume`、
  `POST /api/session/new`。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

覆盖协议层、TUI、GUI 桥接层、**GUI 的 HTTP 层**。
桥接层和 HTTP 层都用假 client 测，不需要图形环境；真起子进程的端到端验证
放在 `scripts/` 下手动跑（`e2e.py`），不进 pytest ——
它依赖 Ollama 和图形环境，进 CI 只会随机变红。
