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
| `AGENTD_LLM_BACKEND` | 透传给 agentd：`fake` / `script` / `ollama` / `openai_compat` | `ollama` |
| `AGENTD_OLLAMA_MODEL` | 透传给 agentd | `auto`（见下） |
| `AGENTD_OLLAMA_HOST` | 透传给 agentd | `http://localhost:11434` |
| `AGENTD_SCRIPT_JSON` | 透传给 agentd：`script` 后端的回放脚本（端到端验证用） | 空 |
| `AGENTD_TOOLS` | 透传给 agentd：原生工具范围 `native` / `read_only` / `off` | `native` |
| `AGENTD_TOOLS_APPROVE` | 透传给 agentd：审批策略 `native` / `all` / `none` | `native` |
| `FORGEAGENT_MCP_CONFIG` | MCP 配置文件位置 | `~/.forgeagent/mcp.json` |
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

### MCP（工具调用）

GUI 负责"**声明**要用哪些 MCP server"，agentd 负责"**连**它们、把工具喂给模型、
执行工具"。这是 ACP 的设计：客户端在 `session/new` 里把 `mcpServers` 传过去，
agent 侧自己连。

配置文件（格式沿用 Claude Desktop / WorkBuddy 的习惯）：

```json
{
  "mcpServers": {
    "echo": {
      "command": "python",
      "args": ["/path/to/echo_server.py"],
      "env": { "SOME_TOKEN": "..." }
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}
```

默认位置 `~/.forgeagent/mcp.json`（可用 `FORGEAGENT_MCP_CONFIG` 改）。文件不存在
就是"没配 MCP"，不会报错。侧栏底部会显示连了几个 server；`GET /api/mcp` 给出
路径与名字列表。

**想马上试**：仓库里带了一个示例 server，一条命令生成配置（默认干跑，`--write` 才落盘）：

```bash
python scripts/install_demo_mcp.py            # 看它准备写什么
python scripts/install_demo_mcp.py --write    # 备份已有的 → 写入 demo 配置
```

`command` 会被填成**启动本脚本的那个解释器**（绝对路径）—— 不能写 `python`，
因为这台机器 PATH 上的 `python` 是微软商店的占位别名，一跑就"未安装 Python"。

**想连窗口一起看（不需要 Ollama、不需要会调工具的模型）**：

```bash
.\.venv\Scripts\python.exe scripts\demo_mcp_gui.py            # 弹 Electron 窗口
.\.venv\Scripts\python.exe scripts\demo_mcp_gui.py --serve    # 只起服务，打印 URL 自己开
.\.venv\Scripts\python.exe scripts\demo_mcp_gui.py --model ollama   # 换成真模型（需 ollama serve）
```

它做的事：写一份**临时** `mcp.json`（指向 `examples/echo_mcp_server.py`，不动你
`~/.forgeagent` 里的真配置）、把后端设成 `AGENTD_LLM_BACKEND=script` + 一段剧本
（第 1 步要调 `demo__echo`、第 2 步出正文）、把会话存储设成 memory（不写你的
`~/.agentd`），然后照常起 GUI。发任意一句话就能看到一张**真的**工具卡片，
输出是示例 MCP server 真的返回的。

配好之后直接聊：模型如果决定调工具，界面上会出现**工具卡片**（一次调用一张，
状态从"运行中"变"完成/失败"，下面是工具的真实输出）。没有工具调用时和普通对话
完全一样。

**不用真模型也能验整条链路**（本机 Ollama 不一定在跑，小模型也不一定会调工具）：

```bash
python scripts/mcp_e2e.py                      # 默认 --agentd-repo D:\workspace\Agentd
python scripts/mcp_e2e.py --agentd-repo /path/to/Agentd
```

这个脚本给 agentd 塞 `AGENTD_LLM_BACKEND=script` + 一段回放脚本，让模型"假装"
先要调 `echo__echo` 再出正文，然后真的去连 Agentd 仓库里的测试 echo MCP server。
四跳全过才算通过：读 mcp.json → ACP session/new 带 mcpServers → agentd 连 MCP
server → 工具事件回灌成界面 reducer 认识的 `Turn.tools`。

**两个踩过的坑**（都已在代码里绕开，写自定义前端时要注意）：

1. ACP 的 `McpServerStdio` 把 `args` / `env` 声明成**必填**，`HttpMcpServer` 还要
   `type`。少字段时 SDK **不报错，而是静默把整份 mcpServers 折成 `[]`** ——
   现象是"配了 MCP 但 agentd 说没接到"。所以 `mcp_config.py` 宁可补空数组也不省略。
2. ACP SDK 传给 agentd 的是 **pydantic 模型对象，不是 dict**。agentd 侧如果只认
   dict 就会静默跳过，日志还说"接入了 1 个 server"却一个工具都列不出来。
   `McpHub` 现在两种形态都吃。

#### 本地 MCP 预设（time / fetch / git 一键装）

上面的 `install_demo_mcp.py` 装的是仓库自带的 echo 示例。想用**真**的通用 server，
用这个（同样默认干跑）：

```bash
python scripts/install_local_mcp.py --list              # 看有哪些预设
python scripts/install_local_mcp.py                     # 干跑：看计划和将要写的内容
python scripts/install_local_mcp.py --install --write   # 真装（建 venv + pip）+ 真写配置
python scripts/verify_local_mcp.py                      # 验证：真连一遍、真调一次探针
```

| 预设 | pip 包 | 用途 | 备注 |
|---|---|---|---|
| `time` | `mcp-server-time` | 时间/时区换算、时间加减 | |
| `fetch` | `mcp-server-fetch` | 抓网页转 markdown | |
| `git` | `mcp-server-git` | git status / log / diff / show | 需要 PATH 上有 git（见下） |

生成的 `mcp.json` 长这样（`command` 是**专用 venv 的解释器绝对路径**，不是 `python`）：

```json
{
  "mcpServers": {
    "time": {
      "command": "C:\\Users\\<you>\\.forgeagent\\mcp-venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server_time"]
    },
    "git": {
      "command": "C:\\Users\\<you>\\.forgeagent\\mcp-venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server_git"],
      "env": { "PATH": "<git 目录>;<原来的 PATH>" }
    }
  }
}
```

**为什么不照抄网上的写法。** 主流教程写 `"command": "uvx"`（Python server）或
`"command": "npx"`（Node server）。这两种在这台机器上都跑不起来：没有 `uv`/`uvx`；
`npx` 那条被沙箱安全策略直接拦掉（`npm view` 报 `ACCESS_DENIED`）。所以改成
「装进一个专用 venv，`command` 指它的解释器绝对路径，`args` 用 `-m <模块>`」。

**为什么单独一个 venv。** `mcp-server-fetch` 会拖进 httpx / readabilipy / markdownify /
protego 一串依赖；装进项目 `.venv` 有和 GUI 自身依赖打架的风险（测试基线会飘）。
专用 venv 放在 `~/.forgeagent/mcp-venv`，和 `mcp.json` 同目录，互不干扰。

**为什么没有 filesystem 预设。** agentd 的原生工具
（`read_file`/`glob`/`grep`/`write_file`/`edit`）已经把本地文件操作覆盖了；
再加一个 filesystem MCP server，只是让模型多一个选择、多一次审批往返，是负收益。

**git 那条为什么带 `env.PATH`。** MCP SDK 的 stdio 客户端把子进程环境算成
`get_default_environment() | (server.env or {})` —— `PATH` 本来就在默认继承的白名单里，
所以普通 server **不需要**写 `env`。git 是例外：这台机器上 `git` 不在 PATH 上
（只在 `~/.workbuddy/binaries/PortableGit/*/cmd/`），不把它拼进 PATH，
`mcp-server-git` fork 出去的 `git` 子进程会直接找不到。安装脚本会自动探测并写进去。

**验证脚本为什么复用 agentd 的 `McpHub`。** 要验的就是 agentd 实际走的那条路
（连接方式、env 合并规则、`{server}__{tool}` 前缀规则）。自己另写一份精简 MCP 客户端，
很容易"验过了但跑起来还是不通"。

**两个本机实测到的坑：**

- **默认 PyPI 源会被沙箱隧道 502。** 实测 `pip install` 走默认源一直报
  `Tunnel connection failed: 502 Bad Gateway`，换清华源立刻就装上了。
  脚本把这个开关透传给 pip（默认不指定，跟 pip 自身配置走）：
  ```bash
  python scripts/install_local_mcp.py --install --write \
      --index-url https://pypi.tuna.tsinghua.edu.cn/simple
  ```
- **`fetch` 探针需要出网，所以"探针错误"默认只算警告。** 实测 `fetch` 探针会报
  `Failed to fetch robots.txt https://example.com/robots.txt due to a connection issue`
  （那个子进程的出网被拦），但 server 本身连通、工具列表正常。
  `verify_local_mcp.py` 因此把两件事分开：**连不上/列不出工具 = 失败（退出码 1）**；
  **探针返回错误 = 警告（退出码 0）**，要当失败就加 `--strict`。

实测输出（本机 2026-09-14）：

```
[time]  OK   工具 2 个：convert_time, get_current_time
             → {"timezone":"Asia/Shanghai","datetime":"2026-09-14T22:40:47+08:00","day_of_week":"Monday",...}
[fetch] OK   工具 1 个：fetch
             ⚠️ 探针 → [错误] Failed to fetch robots.txt ... connection issue
[git]   OK   工具 12 个：git_add, git_branch, git_checkout, git_commit, git_create_branch,
             git_diff, git_diff_staged, git_diff_unstaged, git_log, git_reset, git_show, git_status
             → Repository status: On branch main ... modified: README.md
```

### 原生工具与审批

除了 MCP，agentd 还自带一组**进程内**的原生工具（不需要任何配置就可用）：
`read_file` / `glob` / `grep` / `write_file` / `edit` / `run_command`。
它们和 MCP 工具合并成同一个 tools 数组喂给模型，界面上看起来都是工具卡片。

| 工具 | 卡片标签 | 要审批吗 |
|---|---|---|
| `read_file` | 读取 | 否 |
| `glob` / `grep` | 搜索 | 否 |
| `write_file` / `edit` | 编辑 | **是** |
| `run_command` | 执行 | **是** |

**审批是这一层最要紧的东西。** 会改变外部状态的动作在执行前会停下，
agentd 通过 ACP 的 `session/request_permission` 反向请求 GUI，界面弹一个框：

- 「允许一次」/「本会话总是允许」/「拒绝」；
- 选了"本会话总是允许"之后，同一个工具名不再打扰（记忆在客户端）；
- 关掉弹窗 / 按 Esc / 超时 = 拒绝，agent 会收到 `[错误] 用户拒绝执行 xxx` 并换条路走，
  整轮对话继续，不会卡住。

只读动作（读/搜）**不弹审批**：每个 `ls` 都弹一次，用户三分钟就学会无脑点允许，
审批本身也就废了。

**不想用 / 想收紧**：这些后端配置直接透传给 agentd：

```bash
AGENTD_TOOLS=read_only forgeagent     # 只留读/搜三个，写和执行全部不给模型
AGENTD_TOOLS=off forgeagent           # 全部关掉，退回纯聊天
AGENTD_TOOLS_APPROVE=all forgeagent   # 非只读动作一律弹审批（更严）
AGENTD_TOOLS_APPROVE=none forgeagent  # 全放行（仅无人值守场景，慎用）
```

原生工具的路径一律限制在**会话工作目录**内（`FORGEAGENT_CWD`），`..` 会被
`resolve` 展开后再判越界。要放开得显式设 `AGENTD_TOOLS_ALLOW_OUTSIDE=true`。

**不用真模型验这条链路**：

```bash
python scripts/native_tools_e2e.py          # 允许：文件真被写出来
python scripts/native_tools_e2e.py --deny   # 拒绝：文件绝不能存在
```

它给 agentd 塞回放脚本，让"模型"依次调 `read_file` + `glob`（不该弹审批）、
`write_file`（该弹审批），然后断言：只弹了一次审批、kind 分别是
read/search/edit、允许时文件真的落盘 / 拒绝时文件绝不存在、拒绝被还原成
`cancelled` 而不是 `failed`。

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
    mcp_config.py   读 mcp.json → 转成 ACP session/new 要的 mcpServers 结构
    mcp_presets.py  本地 MCP server 预设：venv 路径推断 / 条目生成 / 配置合并
    assets/         HTML 前端（HTML/CSS/JS，无外部依赖、离线可用）
    __main__.py     GUI 入口（--mode 选前端载体）
electron/          Electron 壳（main.js + package.json），默认前端载体：
                  自带 Chromium 渲染 assets/index.html，参考 WorkBuddy 的前端形态
examples/
  echo_mcp_server.py   示例 MCP server（echo / now 两个工具），试用 MCP 用
scripts/
  e2e.py            三跳验证（Ollama / agentd / TUI 界面）
  mcp_e2e.py        MCP 端到端验证（不用真模型：script 后端 + 真 stdio MCP server）
  native_tools_e2e.py  原生工具 + 审批端到端验证（--deny 走拒绝路径）
  demo_mcp_gui.py   一键开「能看见工具卡片」的 GUI（不用 Ollama；可选 --serve / --model）
  install_demo_mcp.py  把示例 MCP server 写进 ~/.forgeagent/mcp.json（默认干跑）
  install_local_mcp.py 一键装本地 MCP server（time/fetch/git）+ 写配置（默认干跑）
  verify_local_mcp.py  验证本地 MCP server：复用 agentd 的 McpHub 真连 + 真调探针
tests/
  test_acp_client.py   reducer 单测 + 反向请求（审批）单测，不用起进程
  test_app.py          TUI 冒烟（headless）
  test_gui_bridge.py   桥接层单测（注入假 client，无需图形环境）
  test_gui_server.py   本机 UI 服务单测：真起 HTTP 服务，真发请求
  test_gui_mcp_config.py  mcp.json 解析单测（含"必填字段不能省"的回归）
  test_mcp_presets.py     本地 MCP 预设单测：venv 路径 / 条目必填字段 / 配置合并
  test_end_to_end.py   真起子进程跑一遍完整握手 + 流式 + 审批往返
  fake_agent.py        假的 ACP agent（含反向请求），端到端测试用
```

**为什么要分这么多层：** 窗口必须有图形环境才能跑，CI 里测不了；但只要把界面
和 Python 之间的通信做成 HTTP（`server.py`），这一跳就能脱离 GUI 测了 ——
`test_gui_server.py` 真起服务、真发请求，把「前端那一跳」验得干干净净。
再往下一层，`bridge.py` 里一行 GUI 代码都没有，塞个假的 async client 就能测全部逻辑。

### 几个不显然的设计决定

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

**6. 反向请求必须回帧，而且不能靠 id 判方向。**
ACP 不是客户端单向发命令：agent 会反过来请求客户端（审批、读文件、开终端）。
这些是 **JSON-RPC 请求**，每个都必须回一帧 —— 不回，agentd 那边就永久卡在
`await` 上，而且两边日志都干干净净。所以：

- `_read_stdout` 按**有没有 `method` 字段**判断方向，不是按 `id`。两个方向各自
  从 0/1 开始编号，**id 必然撞车**（实测 agentd 的审批请求就是 id=0、1、2…）。
  按 id 判会把审批请求当成 `session/prompt` 的响应 —— prompt 提前结束、
  审批永远没人回、整轮静默挂死。这条有专门的回归测试。
- 没实现的协议扩展（`fs/read_text_file` 之类）也必须回 `-32601`，不能静默丢掉。
- 没有处理器 / 处理器抛异常 / 用户关掉弹窗 / 超时，**一律按拒绝回帧**。方向不能反：
  反了就是"审批通道一坏，所有写操作自动放行"。

**7. `DENY_MARK`：把"用户拒绝"从 `failed` 里救回来。**
ACP 的 `ToolCallStatus` 只有 `pending / in_progress / completed / failed`，
**没有 cancelled**。内核里"用户拒绝"这个状态到了协议层被迫折成 `failed` ——
于是"你点的拒绝"和"命令真的炸了"在协议上长得一模一样。前端只能靠输出文案
（`acp_client.DENY_MARK`）把它还原成 `cancelled`，界面才会显示虚线灰边的
「已取消」而不是红色「失败」。同样是两个仓库共享的字面量。

## 当前限制

- **MCP 工具 + 原生工具都可用**（见上面的「MCP（工具调用）」与「原生工具与审批」）：
  agentd 用 `agent` 模式跑工具循环、GUI 渲染工具卡片并**弹审批框**。
  **没做**的是：diff 视图、工具调用的中途取消（`session/cancel` 目前只是通知）。
- **工具卡片是本轮内的临时状态**，不进历史。刷新/续聊只重放落库的 user/assistant
  文本，工具卡片不会重现（内核只把最终回复落库，不存中间的 tool 往返）。
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

覆盖协议层、TUI、GUI 桥接层、**GUI 的 HTTP 层**、反向请求（审批）、本地 MCP 预设生成。
桥接层和 HTTP 层都用假 client 测，不需要图形环境；真起子进程的端到端验证
放在 `scripts/` 下手动跑（`e2e.py` / `mcp_e2e.py` / `native_tools_e2e.py`），
不进 pytest —— 它们依赖 Ollama 或外部仓库，进 CI 只会随机变红。

`test_end_to_end.py` 是例外：它用 `tests/fake_agent.py` 顶替 agentd，
所以既真起子进程、又不依赖外部仓库，能进 CI。
