"""进程内后端状态：会话存储、任务表、智能体运行时。"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Set

from . import config
from .agent_runtime import GenerationRuntime
from .jobs import JobManager
from .store import SessionStore


class BackendState:
    def __init__(self) -> None:
        self.store = SessionStore(config.sessions_root())
        self.jobs = JobManager()
        self.runtime = GenerationRuntime(self.store)
        # 监听配置在启动时解析一次：是否允许远程访问由 setting.json 决定
        self.web = config.resolve_web_settings()
        self.tasks: Set[asyncio.Task] = set()


@lru_cache(maxsize=1)
def backend_state() -> BackendState:
    return BackendState()


__all__ = ["BackendState", "backend_state"]
