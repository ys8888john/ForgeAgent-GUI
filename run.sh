#!/bin/bash
# ForgeAgent GUI 一键启动（macOS / Linux）
#
# 为什么不能直接 ./electron/node_modules/.bin/electron ./electron：
#   main.js 会找 FORGEAGENT_PYTHON（跑 forgeagent.gui 的解释器），不设就用系统
#   python3 —— 那个解释器里多半没装 forgeagent；而 agentd 装在 Agentd/.venv，
#   又得靠 FORGEAGENT_AGENT_CMD 指过去。这个脚本把两件事都固化好。
#
# 用法：./run.sh          （agentd 的工作目录 = 你执行命令时所在的目录）
#       ./run.sh /path    （显式指定 agentd 的工作目录）
set -euo pipefail
cd "$(dirname "$0")"

GUI_PY="$PWD/.venv/bin/python"
AGENTD_PY="$(cd .. && pwd)/Agentd/.venv/bin/python"

[ -x "$GUI_PY" ] || { echo "缺 $GUI_PY —— 先在项目根执行: uv venv --python 3.12 .venv && uv pip install -e '.[dev]' --python .venv/bin/python"; exit 1; }
[ -x "$AGENTD_PY" ] || { echo "缺 $AGENTD_PY —— 先在 ../Agentd 执行: uv venv --python 3.12 .venv && uv pip install -e '.[dev]' --python .venv/bin/python"; exit 1; }

export FORGEAGENT_PYTHON="$GUI_PY"
export FORGEAGENT_AGENT_CMD="$AGENTD_PY -m agentd.server"
export FORGEAGENT_CWD="${1:-$PWD}"

exec ./electron/node_modules/.bin/electron ./electron
