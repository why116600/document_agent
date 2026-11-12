"""管理入口来源保护：仅回环 + 跨源写请求拒绝。不做 token/角色。"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

# testclient 仅用于 Starlette TestClient，真实 ASGI 服务不会产生这个 client host
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AdminLocalOnlyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = scope.get("client") or ("", 0)
        if not _is_loopback(str(client[0])):
            return await _send_json(send, 403, {
                "detail": "管理入口仅允许本机访问",
                "code": "ADMIN_LOCAL_ONLY",
            })
        return await self.app(scope, receive, send)


class AdminOriginGuardMiddleware:
    """只拦写请求的跨源提交；同源或无 Origin 的本机请求放行。"""

    def __init__(self, app: ASGIApp, allowed_origins: tuple[str, ...] = ()) -> None:
        self.app = app
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in _SAFE_METHODS:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        origin = (headers.get("origin") or "").strip()
        referer = (headers.get("referer") or "").strip()
        source = origin or referer
        if source:
            host = urlparse(source).hostname or ""
            if not _is_loopback(host) and source not in self.allowed_origins:
                return await _send_json(send, 403, {
                    "detail": "管理入口拒绝跨源写请求",
                    "code": "ADMIN_ORIGIN_REJECTED",
                })
        return await self.app(scope, receive, send)


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


async def _send_json(send: Send, status: int, payload: dict) -> None:
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


__all__ = ["AdminLocalOnlyMiddleware", "AdminOriginGuardMiddleware"]
