"""运行时入口路径白名单：/api/* 与公共静态资源。"""

from __future__ import annotations

import re

RUNTIME_RULES = (
    ("GET", re.compile(r"^/api/health$")),
    ("GET", re.compile(r"^/api/sessions$")),
    ("POST", re.compile(r"^/api/sessions$")),
    ("GET", re.compile(r"^/api/sessions/[^/]+$")),
    ("PATCH", re.compile(r"^/api/sessions/[^/]+$")),
    ("DELETE", re.compile(r"^/api/sessions/[^/]+$")),
    ("POST", re.compile(r"^/api/sessions/[^/]+/files$")),
    ("GET", re.compile(r"^/api/sessions/[^/]+/files/.+$")),
    ("DELETE", re.compile(r"^/api/sessions/[^/]+/files/.+$")),
    ("POST", re.compile(r"^/api/sessions/[^/]+/messages$")),
    ("GET", re.compile(r"^/api/jobs/[^/]+$")),
    ("GET", re.compile(r"^/api/jobs/[^/]+/events$")),
    ("GET", re.compile(r"^/api/sessions/[^/]+/logs/[^/]+$")),
    # 飞书版入口：页面与按用户隔离的接口
    ("GET", re.compile(r"^/feishu/?$")),
    ("GET", re.compile(r"^/api/feishu/.+$")),
    ("POST", re.compile(r"^/api/feishu/.+$")),
    ("PATCH", re.compile(r"^/api/feishu/.+$")),
    ("DELETE", re.compile(r"^/api/feishu/.+$")),
    # FastAPI 自带文档；保留以兼容现有调试习惯
    ("GET", re.compile(r"^/docs$")),
    ("GET", re.compile(r"^/redoc$")),
    ("GET", re.compile(r"^/openapi\.json$")),
    ("GET", re.compile(r"^/$")),
    ("GET", re.compile(r"^/favicon\.ico$")),
    ("GET", re.compile(r"^/static/.+$")),
)

__all__ = ["RUNTIME_RULES"]
