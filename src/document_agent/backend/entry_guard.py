"""双入口路由隔离：路径白名单 + 跨入口 403。"""

from __future__ import annotations

import json
import re
from typing import Iterable, Pattern, Sequence, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

_RULE = Tuple[str, Pattern[str]]
_ILLEGAL_PATH = re.compile(r"(\\|%2e%2e|%2f|%5c|//|(?:^|/)\.\.(?:/|$))")


class EntryGuardMiddleware:
    """只做路径/方法判定，来源限制由 AdminLocalOnlyMiddleware 负责。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        entry: str,
        rules: Sequence[_RULE],
        passthrough_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.entry = entry
        self.rules = tuple(rules)
        self.passthrough_prefixes = tuple(passthrough_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path") or "/"
        root_path = scope.get("root_path") or ""
        if root_path and path.startswith(root_path):
            path = path[len(root_path):] or "/"
        method = (scope.get("method") or "GET").upper()
        request_id = scope.setdefault("state", {}).get("request_id", "")

        if _ILLEGAL_PATH.search(path.lower()):
            return await self._send(send, 403, {
                "detail": "路径包含非法片段",
                "code": "ENTRY_PATH_REJECTED",
                "entry": self.entry,
                "method": method,
                "path": path,
                "request_id": request_id,
            })

        if method == "OPTIONS":
            return await self.app(scope, receive, send)

        if any(path == prefix or path.startswith(prefix + "/") for prefix in self.passthrough_prefixes):
            return await self.app(scope, receive, send)

        if self._matches(method, path):
            return await self.app(scope, receive, send)

        status = 405 if self._path_known(path) else 403
        code = "METHOD_NOT_ALLOWED" if status == 405 else "ENTRY_FORBIDDEN"
        detail = "该入口不支持此方法" if status == 405 else "跨入口访问被拒绝"
        return await self._send(send, status, {
            "detail": detail,
            "code": code,
            "entry": self.entry,
            "method": method,
            "path": path,
            "request_id": request_id,
        })

    def _matches(self, method: str, path: str) -> bool:
        return any(method == allowed and pattern.match(path) for allowed, pattern in self.rules)

    def _path_known(self, path: str) -> bool:
        return any(pattern.match(path) for _, pattern in self.rules)

    async def _send(self, send: Send, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


__all__ = ["EntryGuardMiddleware"]
