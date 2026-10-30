"""FastAPI 应用：会话、文件、生成任务与前端托管。

接口一览（全部以 ``/api`` 开头）：
    GET    /api/health                          服务与知识库状态
    GET    /api/sessions                        会话列表
    POST   /api/sessions                        新建会话（自动创建文件夹）
    GET    /api/sessions/{sid}                  会话详情（文件 + 消息）
    PATCH  /api/sessions/{sid}                  重命名会话
    DELETE /api/sessions/{sid}                  删除会话（连同文件夹）
    POST   /api/sessions/{sid}/files            上传参考文件（可多选）
    GET    /api/sessions/{sid}/files/{name}     下载文件
    DELETE /api/sessions/{sid}/files/{name}     删除文件
    POST   /api/sessions/{sid}/messages         提交写作需求，创建后台任务
    GET    /api/jobs/{job_id}                   任务状态
    GET    /api/jobs/{job_id}/events            任务事件流（SSE）
    GET    /api/sessions/{sid}/logs/{job_id}    任务运行日志
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, config
from .agent_runtime import GenerationRuntime
from .jobs import Job, JobManager, install_stdout_router
from .store import InvalidFileName, SessionError, SessionNotFound, SessionStore


class UTF8JSONResponse(JSONResponse):
    """显式声明 UTF-8，避免部分客户端（如 Windows PowerShell 5.1）按 Latin-1 解码中文。"""

    media_type = "application/json; charset=utf-8"


# --------------------------------------------------------------------------- 应用状态
class BackendState:
    """进程内的单例状态：会话存储、任务表、智能体运行时。"""

    def __init__(self) -> None:
        self.store = SessionStore(config.sessions_root())
        self.jobs = JobManager()
        self.runtime = GenerationRuntime(self.store)
        self.tasks: Set[asyncio.Task] = set()


@lru_cache(maxsize=1)
def backend_state() -> BackendState:
    return BackendState()


# --------------------------------------------------------------------------- 请求模型
class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


class MessageCreate(BaseModel):
    content: str


# --------------------------------------------------------------------------- 工具函数
def _file_stream_size(upload: UploadFile) -> int:
    stream = upload.file
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    return size


def _require_session(session_id: str) -> None:
    backend_state().store.session_dir(session_id)


# --------------------------------------------------------------------------- 应用生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    install_stdout_router()
    state = backend_state()
    recovered = state.store.recover_interrupted()
    state.runtime.start()
    url = config.server_url(config.DEFAULT_HOST, config.DEFAULT_PORT)
    print("=" * 72)
    print(f"文档智能体 Web 服务已启动（v{__version__}）")
    print(f"  访问地址：{url}")
    print(f"  工作区：  {state.store.root}")
    print(f"  大模型：  {'已配置' if config.llm_configured() else '未配置 MODEL_API_KEY'}")
    print(f"  知识库：  {state.runtime.knowledge.status()['message']}")
    if recovered:
        print(f"  已处理 {recovered} 个因上次退出而中断的任务")
    print("=" * 72)
    try:
        yield
    finally:
        for task in list(state.tasks):
            task.cancel()
        state.runtime.close()


app = FastAPI(
    title="文档智能体 Web 服务",
    version=__version__,
    description="在浏览器里新建会话、上传参考资料、生成或改写 Word 文档",
    default_response_class=UTF8JSONResponse,
    lifespan=lifespan,
)

_origins = config.cors_origins()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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


# --------------------------------------------------------------------------- 健康检查
@app.get("/api/health")
def health() -> Dict[str, Any]:
    state = backend_state()
    data = state.runtime.health()
    data.update(
        {
            "version": __version__,
            "session_count": len(state.store.list_sessions()),
        }
    )
    return data


# --------------------------------------------------------------------------- 会话
@app.get("/api/sessions")
def list_sessions() -> Dict[str, Any]:
    return {"sessions": backend_state().store.list_sessions()}


@app.post("/api/sessions", status_code=201)
def create_session(payload: Optional[SessionCreate] = None) -> Dict[str, Any]:
    title = payload.title if payload else None
    return backend_state().store.create(title)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    return backend_state().store.detail(session_id)


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, payload: SessionRename) -> Dict[str, Any]:
    return backend_state().store.rename(session_id, payload.title)


@app.delete("/api/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    state = backend_state()
    active = state.jobs.active_for_session(session_id)
    if active is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成文档，请等任务结束后再删除")
    state.runtime.forget(session_id)
    state.jobs.forget_session(session_id)
    state.store.delete(session_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- 文件
@app.post("/api/sessions/{session_id}/files", status_code=201)
async def upload_files(session_id: str, files: List[UploadFile] = File(...)) -> Dict[str, Any]:
    state = backend_state()
    store = state.store
    _require_session(session_id)
    if not files:
        raise HTTPException(status_code=400, detail="没有收到任何文件")
    if len(files) > config.MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多上传 {config.MAX_UPLOAD_FILES} 个文件",
        )

    saved: List[Dict[str, Any]] = []
    errors: List[str] = []
    for upload in files:
        name = upload.filename or ""
        try:
            if not name:
                continue
            size = await run_in_threadpool(_file_stream_size, upload)
            if size <= 0:
                errors.append(f"文件为空：{name}")
                continue
            if size > config.MAX_UPLOAD_BYTES:
                limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
                errors.append(f"文件超过 {limit_mb} MB 上限：{name}")
                continue
            entry = await run_in_threadpool(store.save_upload, session_id, name, upload.file)
            saved.append(entry)
        except InvalidFileName as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"保存 {name or '文件'} 失败：{exc}")
        finally:
            await upload.close()

    if not saved and errors:
        raise HTTPException(status_code=400, detail="；".join(errors))
    return {"files": saved, "errors": errors}


@app.get("/api/sessions/{session_id}/files/{name:path}")
def download_file(session_id: str, name: str) -> FileResponse:
    path = backend_state().store.resolve_file(session_id, name)
    return FileResponse(path, filename=path.name)


@app.delete("/api/sessions/{session_id}/files/{name:path}", status_code=204)
def delete_file(session_id: str, name: str) -> Response:
    backend_state().store.delete_file(session_id, name)
    return Response(status_code=204)


# --------------------------------------------------------------------------- 生成任务
async def _run_job(
    state: BackendState,
    job: Job,
    session_id: str,
    prompt: str,
    assistant_message_id: str,
) -> None:
    """把阻塞式的智能体调用放到线程池执行，避免卡住事件循环。"""
    try:
        await asyncio.to_thread(
            state.runtime.run_generation,
            job,
            session_id,
            prompt,
            assistant_message_id,
        )
    except asyncio.CancelledError:
        # 线程仍会执行自己的 finally（写日志、释放会话锁），这里只记录任务被取消
        job.fail("服务正在关闭，任务被中断")
        raise
    except Exception as exc:  # to_thread 本身抛出异常时的兜底
        job.fail(str(exc) or exc.__class__.__name__)


@app.post("/api/sessions/{session_id}/messages", status_code=202)
async def create_message(session_id: str, payload: MessageCreate) -> Dict[str, Any]:
    state = backend_state()
    store = state.store
    text = (payload.content or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="写作需求不能为空")
    _require_session(session_id)

    if state.jobs.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成文档，请等待任务结束后再发送")
    if not state.runtime.try_begin(session_id):
        raise HTTPException(status_code=409, detail="当前会话正在生成文档，请稍候")

    try:
        # 首个需求直接用作会话标题，避免会话列表里全是“会话 xx:xx”
        messages = store.list_messages(session_id)
        meta = store.meta(session_id)
        if not messages and str(meta.get("title") or "").startswith("会话 "):
            store.rename(session_id, text[:24])

        user_message = store.append_message(
            session_id,
            {"role": "user", "content": text, "status": "done"},
        )
        job = state.jobs.create(session_id)
        assistant_message = store.append_message(
            session_id,
            {
                "role": "assistant",
                "content": "",
                "status": "running",
                "job_id": job.id,
            },
        )
    except Exception:
        # 建任务失败时必须把会话锁还回去，否则该会话再也发不出消息
        try:
            state.runtime.session_lock(session_id).release()
        except RuntimeError:
            pass
        raise

    task = asyncio.create_task(
        _run_job(state, job, session_id, text, assistant_message["id"]),
        name=f"doc-agent-{job.id}",
    )
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)

    return {
        "job_id": job.id,
        "message_id": assistant_message["id"],
        "user_message_id": user_message["id"],
        "session": store.meta(session_id),
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    job = backend_state().jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job.summary()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    state = backend_state()
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")

    # 断线重连时浏览器会带上 Last-Event-ID，从这里继续推送，避免日志重复
    cursor = 0
    raw_last_id = request.headers.get("last-event-id")
    if raw_last_id:
        try:
            cursor = max(0, int(raw_last_id) + 1)
        except ValueError:
            cursor = 0

    async def event_stream():
        nonlocal cursor
        while True:
            events, next_cursor, done = job.snapshot(cursor)
            for offset, event in enumerate(events):
                event_id = cursor + offset
                yield f"id: {event_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    done = True
            cursor = next_cursor
            if done:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.15)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/sessions/{session_id}/logs/{job_id}", response_class=PlainTextResponse)
def job_log(session_id: str, job_id: str) -> PlainTextResponse:
    path = backend_state().store.log_path(session_id, job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="运行日志不存在")
    return PlainTextResponse(
        path.read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


# --------------------------------------------------------------------------- 前端静态资源
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
