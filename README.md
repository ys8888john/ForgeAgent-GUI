# ForgeAgent-GUI

面向 [ACP](https://agentclientprotocol.com)（Agent Client Protocol）agent 的终端客户端，
用来跟 `agentd` 对话。

**它不是 agent 的一部分。** 它通过 stdio 把 agentd 当子进程拉起来，用 JSON-RPC 通信——
跟 Zed、JetBrains 那些客户端是同一个姿势。所以换 UI 不用动内核，换内核不用动 UI。

## 跑起来

```bash
cd /root/workspace/ForgeAgent-GUI
pip install -e .

# agentd 在另一个仓库，用 PYTHONPATH 指过去
PYTHONPATH=/root/workspace/Agentd forgeagent
```

也可以不装，直接跑：

```bash
cd /root/workspace/ForgeAgent-GUI
PYTHONPATH=/root/workspace/Agentd:. python -m forgeagent
```

### 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `FORGEAGENT_AGENT_CMD` | 拉起 agent 的命令 | `python -m agentd.server` |
| `FORGEAGENT_CWD` | agent 的工作目录 | 当前目录 |
| `AGENTD_LLM_BACKEND` | 透传给 agentd：`fake` / `ollama` / `openai_compat` | — |

不连真模型先验链路：

```bash
AGENTD_LLM_BACKEND=fake \
PYTHONPATH=/root/workspace/Agentd forgeagent
```

接 Ollama：

```bash
AGENTD_LLM_BACKEND=ollama \
PYTHONPATH=/root/workspace/Agentd forgeagent
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
  acp_client.py   协议层：拉起子进程 + JSON-RPC + 事件折叠。只依赖标准库
  app.py          UI 层：Textual 界面。这才是需要 Textual 的地方
  __main__.py     入口
tests/
  test_acp_client.py   reducer 单测，不用起进程
```

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

## 当前限制

- **只有文本流。** 工具调用卡片、diff、权限弹窗都没做——不是 UI 写不了，
  是 agentd 目前根本不产出这些事件（`acp_stdio.py` 里明确写着"工具调用类事件还没映射"）。
  内核补齐后，挂卡片的位置在 `acp_client.py` 的 `_apply_update` 里，已留好分支。
- **纯文本渲染**，没上 Markdown。流式刷 Markdown 会闪，等文本长了再说。
- 单会话，不持久化（跟着内核走，内核做 SQLite 这里才有得存）。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

测试只覆盖协议层，**不需要 Textual，也不启子进程**。
