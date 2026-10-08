@echo off
REM ForgeAgent GUI 启动器（Windows）
REM
REM agentd 在 Windows 本地运行；大模型由 WSL2 内的 Ollama 提供。
REM
REM 网络模式：%USERPROFILE%\.wslconfig 里 networkingMode=mirrored。WSL 与宿主共用
REM 同一个网络栈，WSL 内监听 0.0.0.0:11434 的 Ollama 直接出现在宿主的 0.0.0.0
REM 上，所以 **http://localhost:11434 永久可连、地址永不改变**（不再有
REM 172.18.x.x 那种每次 WSL 重启都可能变的 NAT 地址）。
REM
REM 前提：WSL 内 ollama 的 systemd 服务需设 OLLAMA_HOST=0.0.0.0:11434
REM （已配），否则只绑 127.0.0.1，mirrored 下宿主机也访问不到。
REM 改 .wslconfig 后必须 wsl --shutdown 才生效。
REM
REM GUI 行为（见 forgeagent/gui/server.py 的 _route_wsl_ollama / _wsl_mirrored）：
REM profile id 含 "wsl" 时，mirrored 下保持 loopback 原样不改写，只负责把停掉
REM 的 WSL 拉起来；哪天退回 nat 模式，则自动改写成 WSL 当前的 IP（那种模式下
REM localhost 转发实测不生效，只能直连 IP）。两种模式都不需要手改配置。
REM
REM 别再手工加 portproxy 规则（netsh interface portproxy add ...）：WSL 的 IP
REM 每次重启都会变，静态规则第二次启动就指向空地址，表现为「连不上 Ollama」。
REM 要清掉历史遗留的坏规则：
REM   netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=11434
REM
REM 前置条件（一次性）：WSL 里已装 Ollama，且拉好模型（如 qwen3:8b）。
REM GUI 启动时会自动拉起停掉的 WSL（Ollama 靠 WSL 内 systemd 随发行区启动）。
REM
REM 用法：
REM   run-windows.bat                以"当前目录"为一个空间（Space）打开
REM   run-windows.bat D:\your\project  把指定目录作为一个空间打开
REM 说明：显式 --cwd 会被当成空间打开/创建并激活；想用默认空间
REM       (~/.agentd/spaces/default) 请用 forgeagent-gui --mode serve（不带 --cwd）。
REM
REM 想用 Electron 窗口（而非浏览器）：先把 electron/ 依赖装好
REM （cd electron && npm install），再把下面 --mode serve 改成 --mode electron。

setlocal
if "%~1"=="" (set AGENT_CWD=%CD%) else (set AGENT_CWD=%~1)
set FORGEAGENT_CWD=%AGENT_CWD%

set "GUI_PY=D:\workspace\ForgeAgent-GUI\.venv\Scripts\python.exe"
if not exist "%GUI_PY%" (
  echo 缺少 %GUI_PY%
  echo 先在项目根执行： python -m venv .venv 然后 .venv\Scripts\pip install -e ".[dev]"
  pause
  exit /b 1
)

echo 启动 ForgeAgent GUI（serve 模式）…… 把下面的 URL 粘到浏览器打开。
"%GUI_PY%" -m forgeagent.gui --mode serve --cwd "%AGENT_CWD%"
endlocal
