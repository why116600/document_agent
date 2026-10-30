"""导入路径引导。

智能体节点用的是「散装模块」导入（``from llm_client import ...``），控制台程序
``console_app.py`` 通过 ``sys.path.append`` 解决。Web 后端同样需要把 ``src``、
``src/document_agent/agent``、``src/document_agent/retrieve_tool`` 三个目录加入
``sys.path``，这里统一处理，避免各处重复。
"""

from __future__ import annotations

import sys
from pathlib import Path


def src_dir() -> Path:
    """返回 ``src`` 目录（backend/bootstrap.py -> backend -> document_agent -> src）。"""
    return Path(__file__).resolve().parents[2]


def ensure_import_paths() -> None:
    """把项目源码目录加入 ``sys.path``，可重复调用。"""
    root = src_dir()
    for path in (
        root,
        root / "document_agent" / "agent",
        root / "document_agent" / "retrieve_tool",
    ):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
