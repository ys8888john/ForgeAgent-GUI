"""极简假 ACP agent —— 只服务于测试，不参与打包。

存在的意义：让客户端的端到端测试**不依赖真实的 agentd**。
不装 Textual、不进 WSL2、不起 Ollama，也能验证 spawn + 握手 + 流式这套链路。

用法：它只读 stdin 的 NDJSON、只往 stdout 写 NDJSON，日志走 stderr。

反向请求：prompt 里出现「确认」两个字时，它会先发一个
`session/request_permission`（**是请求，带 id，必须回帧**）并阻塞等答案，
然后把答案写进 thought 通道告诉测试 —— 这样"客户端到底回了什么"是可断言的。
"""

from __future__ import annotations

import json
import sys

# 故意往 stderr 打两行，用来验证客户端确实捕获了 stderr 而不是让它污染终端
print("[fake-agent] 启动", file=sys.stderr, flush=True)

# agent 侧自己发的请求 id 从 1000 起 —— 刻意和客户端从 1 开始的编号错开，
# 但客户端**不该**依赖这个（协议上两边各自编号，撞车是合法的），
# 所以真正的回归测试在 test_acp_client.py 里手动制造 id 撞车。
_next_req_id = 1000


def send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def notify(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def read_until(rid: int) -> dict | None:
    """阻塞读完直到拿到 id == rid 的那一帧（假 agent 是单线程同步的，够用）。"""
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        try:
            msg = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if msg.get("id") == rid:
            return msg


def ask_permission(session_id: str) -> bool:
    """发一个审批请求并等答案，返回用户是否放行。"""
    global _next_req_id
    _next_req_id += 1
    rid = _next_req_id
    send(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "fake-call-1",
                    "title": "run_command",
                    "kind": "execute",
                    "rawInput": {"detail": "command: echo hi"},
                },
                "options": [
                    {"optionId": "allow_once", "name": "允许一次", "kind": "allow_once"},
                    {"optionId": "allow_session", "name": "本会话总是允许", "kind": "allow_always"},
                    {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                ],
            },
        }
    )
    reply = read_until(rid) or {}
    outcome = (reply.get("result") or {}).get("outcome") or {}
    if outcome.get("outcome") != "selected":
        return False
    return str(outcome.get("optionId") or "").startswith("allow")


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
        prompt_text = "".join(
            b.get("text", "") for b in (params.get("prompt") or []) if isinstance(b, dict)
        )

        if "确认" in prompt_text:
            allowed = ask_permission(sid)
            notify(
                "session/update",
                {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {
                            "type": "text",
                            "text": "[perm] " + ("allow" if allowed else "deny"),
                        },
                    },
                },
            )

        for chunk in ("你", "好", "世", "界"):
            notify(
                "session/update",
                {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": chunk},
                    },
                },
            )
        # 再混一条思考通道的，验证分流
        notify(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "我在想"},
                },
            },
        )
        result = {"stopReason": "end_turn"}

    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"未实现: {method}"},
            }
        )
        continue

    send({"jsonrpc": "2.0", "id": mid, "result": result})
