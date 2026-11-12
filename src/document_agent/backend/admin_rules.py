"""管理入口路径白名单（子应用内看到的路径已去掉 /admin）。"""

from __future__ import annotations

import re

ADMIN_RULES = (
    ("GET", re.compile(r"^/api/health$")),
    ("POST", re.compile(r"^/api/skills/draft$")),
    ("POST", re.compile(r"^/api/skills/draft/preview$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/draft$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/draft/preview$")),
    ("GET", re.compile(r"^/api/skills$")),
    ("POST", re.compile(r"^/api/skills$")),
    ("GET", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*$")),
    ("PUT", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*$")),
    ("PATCH", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*$")),
    ("DELETE", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/validate$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/enable$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/disable$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/publish$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/rollback$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/restore$")),
    ("POST", re.compile(r"^/api/skills/migrate$")),
    ("GET", re.compile(r"^/api/runtime/diagnostics$")),
    ("GET", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/versions$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/references$")),
    ("DELETE", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/references/.+$")),
    ("POST", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/template$")),
    ("DELETE", re.compile(r"^/api/skills/[a-z0-9][a-z0-9_-]*/template$")),
    ("GET", re.compile(r"^/api/audit$")),
)

__all__ = ["ADMIN_RULES"]
