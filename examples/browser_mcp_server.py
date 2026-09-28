"""浏览器 MCP server —— 把"搜索/网页"闭环的最后一步交回用户的浏览器。

为什么是"打开"而不是"我去看"：
    agentd 已有原生 web_search（Bing 检索）与 web_fetch（抓正文转文本）——
    模型自己读网页的路已经通了。这里补的是那类产品（WorkBuddy 等）常见且
    只能由客户端做的最后一步：**把链接/搜索结果页在用户的真实浏览器里打开**
    （让用户登录、看 JS 页面、继续人工浏览——这些都是 MCP server 替代不了
    的）。跨平台依赖只有 Python 标准库的 webbrowser。

与安全相关的取舍：
1. 只接受 http/https 链接（file:/javascript: 不可能被打开）；
2. open_url / open_search 都**不声明只读**（打开页面是外向动作），
   agentd 会把它归为 execute kind → 每次都经过审批弹窗；
3. `--stub` 只用于测试/CI：打印将要打开的 URL 而不真的拉起浏览器。

工具：
    open_url(url)                      在默认浏览器打开网页
    open_search(query, engine="bing")  打开搜索引擎的结果页（bing/google/duckduckgo）
"""

from __future__ import annotations

import argparse
import urllib.parse
import webbrowser

from mcp.server.mcpserver import MCPServer

server = MCPServer("browser")

_state: dict = {"stub": False}

_SEARCH_ENGINES = {
    "bing": "https://www.bing.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
}


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL 不能为空")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("只接受 http/https 链接")
    if any(ch in url for ch in (" ", "\n", "\t")):
        raise ValueError("URL 里包含不允许的空白字符")
    return url


def _open(url: str) -> str:
    if _state["stub"]:
        return f"[stub] 将打开：{url}"
    if webbrowser.open(url):
        return f"已在默认浏览器打开：{url}"
    return f"[错误] 浏览器打开失败（系统没有可用的浏览器）：{url}"


@server.tool()
def open_url(url: str) -> str:
    """在用户默认浏览器里打开一个网页链接（用于需要人工查看/登录的页面）。"""
    try:
        url = _normalize_url(url)
    except ValueError as exc:
        return f"[错误] {exc}"
    return _open(url)


@server.tool()
def open_search(query: str, engine: str = "bing") -> str:
    """在用户的浏览器里打开一次网页搜索的结果页（engine: bing/google/duckduckgo）。"""
    query = (query or "").strip()
    if not query:
        return "[错误] 搜索词不能为空"
    engine = (engine or "bing").strip().lower()
    if engine not in _SEARCH_ENGINES:
        return f"[错误] 不支持的搜索引擎：{engine}（可用：{', '.join(_SEARCH_ENGINES)}）"
    url = _SEARCH_ENGINES[engine].format(q=urllib.parse.quote_plus(query))
    return _open(url)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ForgeAgent 浏览器 MCP server")
    ap.add_argument("--stub", action="store_true", help="测试模式：不真的打开浏览器，只返回将打开的 URL")
    args = ap.parse_args()
    _state["stub"] = bool(args.stub)
    server.run("stdio")
