"""Skills 管理控制台子应用：/admin/api/*，仅本机可用，无自然语言能力。"""

from __future__ import annotations

from fastapi import FastAPI, Request

from document_agent.skills.admin.service import (
    AdminConflict,
    AdminError,
    AdminNotFound,
    AdminValidationError,
)

from .admin_guard import AdminLocalOnlyMiddleware, AdminOriginGuardMiddleware
from .admin_routes import router as admin_router
from .admin_rules import ADMIN_RULES
from .entry_guard import EntryGuardMiddleware
from .responses import UTF8JSONResponse


def create_admin_app() -> FastAPI:
    app = FastAPI(
        title="Skills 管理控制台",
        description="结构化 Skill 管理 API；仅本机可用，不提供自然语言交互",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=UTF8JSONResponse,
    )

    # 注意：后添加者先执行；路径守卫必须最外层
    app.add_middleware(AdminOriginGuardMiddleware)
    app.add_middleware(AdminLocalOnlyMiddleware)
    app.add_middleware(
        EntryGuardMiddleware,
        entry="admin",
        rules=ADMIN_RULES,
    )

    @app.exception_handler(AdminNotFound)
    async def _not_found(request: Request, exc: AdminNotFound):
        return UTF8JSONResponse(
            {"detail": str(exc), "code": "SKILL_NOT_FOUND"},
            status_code=404,
        )

    @app.exception_handler(AdminConflict)
    async def _conflict(request: Request, exc: AdminConflict):
        return UTF8JSONResponse(
            {"detail": str(exc), "code": "SKILL_CONFLICT"},
            status_code=409,
        )

    @app.exception_handler(AdminValidationError)
    async def _invalid(request: Request, exc: AdminValidationError):
        return UTF8JSONResponse(
            {"detail": str(exc), "code": "SKILL_INVALID"},
            status_code=422,
        )

    @app.exception_handler(AdminError)
    async def _admin_error(request: Request, exc: AdminError):
        return UTF8JSONResponse(
            {"detail": str(exc), "code": "ADMIN_ERROR"},
            status_code=400,
        )

    app.include_router(admin_router)
    return app


__all__ = ["create_admin_app"]
