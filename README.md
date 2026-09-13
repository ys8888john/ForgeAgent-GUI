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
| `FORGEAGENT_AGENT_CMD` | 拉起 agent 的命令 | `sys.executable -m agentd.server` |
| `FORGEAGENT_CWD` | agent 的工作目录 | 当前目录 |
| `AGENTD_LLM_BACKEND` | 透传给 agentd：`fake` / `ollama` / `openai_compat` | `ollama` |
| `AGENTD_OLLAMA_MODEL` | 透传给 agentd | `auto`（见下） |
| `AGENTD_OLLAMA_HOST` | 透传给 agentd | `http://localhost:11434` |

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
  acp_client.py   协议层：拉起子进程 + JSON-RPC + 事件折叠。只依赖标准库
  app.py          UI 层：Textual 界面。这才是需要 Textual 的地方
  __main__.py     入口
scripts/
  e2e.py          三跳验证（Ollama / agentd / 界面）
tests/
  test_acp_client.py   reducer 单测，不用起进程
  test_app.py          UI 层冒烟测试（需要 Textual，headless）
  test_end_to_end.py   真起子进程跑一遍完整握手 + 流式
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
- **纯文本渲染**，没上 Markdown。流式刷 Markdown 会闪，等文本长了再说。
- 单会话，不持久化（跟着内核走，内核做 SQLite 这里才有得存）。

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

测试只覆盖协议层，**不需要 Textual，也不启子进程**。
