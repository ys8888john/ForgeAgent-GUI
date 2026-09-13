"""GUI 子包。

刻意不在 `__init__` 里 import webview —— 只有 window.py 需要它。
这样 bridge 层能脱离 GUI 单测，也能在没有图形环境的地方（CI）导入。
"""

from .bridge import Bridge

__all__ = ["Bridge"]
