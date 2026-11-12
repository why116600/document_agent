"""运行时入口路由：/api/*，只负责会话、文件与写作任务。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, config
from .jobs import Job
from .state import BackendState, backend_state
from .store import InvalidFileName

router = APIRouter()


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


class MessageCreate(BaseModel):
    content: str


def _file_stream_size(upload: UploadFile) -> int:
    stream = upload.file
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    return size


def _require_session(session_id: str) -> None:
    backend_state().store.session_dir(session_id)


@router.get("/health")
def health() -> Dict[str, Any]:
    state = backend_state()
    data = state.runtime.health()
    web = state.web
    data.update(
        {
            "version": __version__,
            "session_count": len(state.store.list_sessions()),
            "remote_access": web["allow_remote"],
            "configured_remote": web.get("configured_remote", web["allow_remote"]),
            "bind_host": web["host"],
            "host_source": web.get("host_source", "config"),
            "port": web["port"],
            "local_url": web["local_url"],
            "remote_urls": web["remote_urls"],
            "config_path": web["config_path"],
        }
    )
    return data


@router.get("/sessions")
def list_sessions() -> Dict[str, Any]:
    return {"sessions": backend_state().store.list_sessions()}


@router.post("/sessions", status_code=201)
def create_session(payload: Optional[SessionCreate] = None) -> Dict[str, Any]:
    title = payload.title if payload else None
    return backend_state().store.create(title)


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    return backend_state().store.detail(session_id)


@router.patch("/sessions/{session_id}")
def rename_session(session_id: str, payload: SessionRename) -> Dict[str, Any]:
    return backend_state().store.rename(session_id, payload.title)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str) -> Response:
    state = backend_state()
    active = state.jobs.active_for_session(session_id)
    if active is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成文档，请等任务结束后再删除")
    state.runtime.forget(session_id)
    state.jobs.forget_session(session_id)
    state.store.delete(session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/files", status_code=201)
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


@router.get("/sessions/{session_id}/files/{name:path}")
def download_file(session_id: str, name: str) -> FileResponse:
    path = backend_state().store.resolve_file(session_id, name)
    return FileResponse(path, filename=path.name)


@router.delete("/sessions/{session_id}/files/{name:path}", status_code=204)
def delete_file(session_id: str, name: str) -> Response:
    backend_state().store.delete_file(session_id, name)
    return Response(status_code=204)


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
        job.fail("服务正在关闭，任务被中断")
        raise
    except Exception as exc:
        job.fail(str(exc) or exc.__class__.__name__)


@router.post("/sessions/{session_id}/messages", status_code=202)
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


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    job = backend_state().jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job.summary()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    state = backend_state()
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")

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


@router.get("/sessions/{session_id}/logs/{job_id}", response_class=PlainTextResponse)
def job_log(session_id: str, job_id: str) -> PlainTextResponse:
    path = backend_state().store.log_path(session_id, job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="运行日志不存在")
    return PlainTextResponse(
        path.read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


__all__ = ["router"]
