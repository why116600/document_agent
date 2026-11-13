"""FastAPI 应用装配：运行时入口 /api 与管理入口 /admin。

接口一览（运行时，全部以 ``/api`` 开头）：
    GET    /api/health
    GET    /api/sessions
    POST   /api/sessions
    GET    /api/sessions/{sid}
    PATCH  /api/sessions/{sid}
    DELETE /api/sessions/{sid}
    POST   /api/sessions/{sid}/files
    GET    /api/sessions/{sid}/files/{name}
    DELETE /api/sessions/{sid}/files/{name}
    POST   /api/sessions/{sid}/messages
    GET    /api/jobs/{job_id}
    GET    /api/jobs/{job_id}/events
    GET    /api/sessions/{sid}/logs/{job_id}

管理入口（仅本机）：
    /admin/api/skills 等，详见 ``backend/admin_routes.py``。

飞书版：页面 ``/feishu``，接口 ``/api/feishu/*``（登录、按用户隔离的会话/文件/任务，
实现见 ``feishu.py``）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .admin_app import create_admin_app
from .entry_guard import EntryGuardMiddleware
from .feishu import router as feishu_router
from .jobs import install_stdout_router
from .request_context import RequestContextMiddleware
from .responses import UTF8JSONResponse
from .runtime_routes import router as runtime_router
from .runtime_rules import RUNTIME_RULES
from .state import BackendState, backend_state
from .store import InvalidFileName, SessionError, SessionNotFound


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_stdout_router()
    state = backend_state()
    recovered = state.store.recover_interrupted()
    recovered_feishu = state.feishu.recover_interrupted()
    state.runtime.start()
    app.state.feishu = state.feishu
    web = state.web
    mode = "允许远程访问" if web["allow_remote"] else "仅本机访问"
    source = "命令行 --host 指定" if web.get("host_source") == "command-line" else "setting.json 配置"
    print("=" * 72)
    print(f"文档智能体 Web 服务已启动（v{__version__}）")
    print(f"  监听地址：{web['host']}:{web['port']}（{mode}，来源：{source}）")
    print(f"  访问地址：{web['local_url']}")
    if web["allow_remote"]:
        if web["remote_urls"]:
            for item in web["remote_urls"]:
                print(f"  远程访问：{item}")
        else:
            print(f"  远程访问：http://<本机局域网IP>:{web['port']}/")
    elif web.get("host_source") != "command-line":
        print('  提示：如需其他主机访问，请在 setting.json 中设置 "web_allow_remote": true 后重启')
    print(f"  工作区：  {state.store.root}")
    feishu_ready = config.feishu_configured()
    feishu_settings = config.feishu_settings()
    print(f"  飞书版：  {web['local_url']}feishu （{'已配置飞书应用' if feishu_ready else '未配置 feishu.app_id/app_secret'}）")
    if feishu_ready:
        if feishu_settings["redirect_uri"]:
            print(f"  飞书回调：{feishu_settings['redirect_uri']}（需与飞书后台登记完全一致）")
        else:
            print(f"  飞书回调：按访问地址自动推导，例如 {web['local_url']}api/feishu/auth/callback")
            print("            请把浏览器实际访问地址对应的回调 URL 添加到飞书应用 → 安全设置 → 重定向 URL")
    print(f"  大模型：  {'已配置' if config.llm_configured() else '未配置 MODEL_API_KEY'}")
    print(f"  管理入口：{'/admin（仅本机）' if config.admin_settings()['enabled'] else '已关闭'}")
    knowledge = state.runtime.knowledge.status()
    print(f"  知识图谱：{knowledge.get('message') or knowledge.get('state')}")
    if knowledge.get("mode") == "remote":
        print("            模式：远程（setting.json 的 remote_kg.enabled=true）")
    if recovered:
        print(f"  已处理 {recovered} 个因上次退出而中断的任务")
    if recovered_feishu:
        print(f"  已处理 {recovered_feishu} 个飞书用户的中断任务")
    print("=" * 72)
    try:
        yield
    finally:
        for task in list(state.tasks):
            task.cancel()
        state.feishu.close()
        state.runtime.close()


app = FastAPI(
    title="文档智能体 Web 服务",
    version=__version__,
    description="在浏览器里新建会话、上传参考资料、生成或改写 Word 文档",
    default_response_class=UTF8JSONResponse,
    lifespan=lifespan,
)


@app.exception_handler(SessionNotFound)
async def _session_not_found(request: Request, exc: SessionNotFound):
    return UTF8JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(InvalidFileName)
async def _invalid_file_name(request: Request, exc: InvalidFileName):
    return UTF8JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(SessionError)
async def _session_error(request: Request, exc: SessionError):
    return UTF8JSONResponse({"detail": str(exc)}, status_code=400)


# --------------------------------------------------------------------------- 路由装配
app.include_router(runtime_router, prefix="/api")

# 飞书版接口（/api/feishu）：登录、用户隔离的会话/文件/任务
app.include_router(feishu_router)


if config.admin_settings()["enabled"]:
    app.mount("/admin", create_admin_app())

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/feishu", include_in_schema=False)
def feishu_index() -> FileResponse:
    """飞书版桌面网页（独立页面，登录后才能使用）。"""
    return FileResponse(config.STATIC_DIR / "feishu" / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------------- 中间件
# 后添加者先执行：CORS -> RequestContext -> EntryGuard -> 路由
app.add_middleware(
    EntryGuardMiddleware,
    entry="runtime",
    rules=RUNTIME_RULES,
    passthrough_prefixes=("/admin",),
)
app.add_middleware(RequestContextMiddleware)

_origins = config.cors_origins()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


__all__ = ["app", "BackendState", "backend_state"]
