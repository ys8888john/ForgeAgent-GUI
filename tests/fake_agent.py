"""极简假 ACP agent —— 只服务于测试，不参与打包。

存在的意义：让客户端的端到端测试**不依赖真实的 agentd**。
不装 Textual、不进 WSL2、不起 Ollama，也能验证 spawn + 握手 + 流式这套链路。

用法：它只读 stdin 的 NDJSON、只往 stdout 写 NDJSON，日志走 stderr。
"""

from __future__ import annotations

import json
import sys

# 故意往 stderr 打两行，用来验证客户端确实捕获了 stderr 而不是让它污染终端
print("[fake-agent] 启动", file=sys.stderr, flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue

    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params", {})

    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}

    elif method == "session/new":
        result = {"sessionId": "test-session-0001"}

    elif method == "session/prompt":
        # 关键：先推几个流式通知，再回最终响应。
        # 顺序必须是"通知在前、响应在后"，客户端就是靠这个区分的。
        sid = params.get("sessionId", "")
        for chunk in ("你", "好", "世", "界"):
            note = {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": chunk},
                    },
                },
            }
            sys.stdout.write(json.dumps(note) + "\n")
            sys.stdout.flush()
        # 再混一条思考通道的，验证分流
        note = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "我在想"},
                },
            },
        }
        sys.stdout.write(json.dumps(note) + "\n")
        sys.stdout.flush()
        result = {"stopReason": "end_turn"}

    else:
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": f"未实现: {method}"},
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        continue

    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()
