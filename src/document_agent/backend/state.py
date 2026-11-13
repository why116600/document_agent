"""进程内后端状态：会话存储、任务表、智能体运行时。"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Set

from . import config
from .agent_runtime import GenerationRuntime
from .feishu import FeishuService
from .jobs import JobManager
from .store import SessionStore


class BackendState:
    def __init__(self) -> None:
        self.store = SessionStore(config.sessions_root())
        self.jobs = JobManager()
        self.runtime = GenerationRuntime(self.store)
        # 监听配置在启动时解析一次：是否允许远程访问由 setting.json 决定
        self.web = config.resolve_web_settings()
        # 飞书版：独立用户工作区，共享同一个知识库运行时
        self.feishu = FeishuService(
            root=config.workspace_root() / "feishu",
            knowledge=self.runtime.knowledge,
            web=self.web,
        )
        self.tasks: Set[asyncio.Task] = set()


@lru_cache(maxsize=1)
def backend_state() -> BackendState:
    return BackendState()


__all__ = ["BackendState", "backend_state"]
