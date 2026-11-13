"""飞书版后端：飞书 OAuth 登录、用户身份与数据隔离。

设计要点：

* **登录**：走飞书网页应用的 OAuth 授权码流程（扫码/已登录飞书账号确认），
  回调后换取 ``user_access_token`` 并调用 ``authen/v1/user_info`` 获取用户名，
  服务端只保存用户 open_id 的签名 Cookie，不保存飞书令牌；
* **隔离**：每个飞书用户都有独立的会话根目录
  ``workspace/feishu/users/<open_id>/sessions``，会话、文件、任务、日志都只在该目录内读写，
  接口从登录 Cookie 推导用户，不接受客户端传入的用户标识；
* **共享**：所有用户共用同一个知识库运行时与智能体依赖，避免重复连接图数据库。

接口统一挂在 ``/api/feishu`` 下，前端页面为 ``/feishu``。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import __version__, config
from .agent_runtime import GenerationRuntime, KnowledgeRuntime
from .jobs import Job, JobManager
from .store import InvalidFileName, SessionStore

SESSION_COOKIE = "docagent_feishu_session"
STATE_COOKIE = "docagent_feishu_state"
SESSION_COOKIE_PATH = "/"
STATE_COOKIE_PATH = "/api/feishu"
SESSION_MAX_AGE = 7 * 24 * 60 * 60
STATE_MAX_AGE = 10 * 60

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


class FeishuAuthError(Exception):
    """飞书登录流程中的可预期错误（不向浏览器暴露内部细节）。"""


@dataclass
class FeishuUser:
    """登录后的飞书用户（只保留展示与隔离需要的字段）。"""

    open_id: str
    name: str
    avatar_url: str = ""
    union_id: str = ""
    user_id: str = ""
    tenant_key: str = ""

    def public(self) -> Dict[str, str]:
        return {"name": self.name, "avatar_url": self.avatar_url}


class FeishuUserWorkspace:
    """单个飞书用户的会话工作区（会话、任务、智能体实例互相隔离）。"""

    def __init__(self, sessions_root: Path, knowledge: KnowledgeRuntime):
        self.sessions_root = sessions_root
        self.store = SessionStore(sessions_root)
        self.jobs = JobManager()
        self.runtime = GenerationRuntime(self.store, knowledge=knowledge)
        self.tasks: Set[asyncio.Task] = set()


class FeishuService:
    """飞书登录、用户档案与用户工作区的管理入口。"""

    def __init__(
        self,
        root: Path,
        knowledge: KnowledgeRuntime,
        web: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.users_root = self.root / "users"
        self.users_root.mkdir(parents=True, exist_ok=True)
        self.knowledge = knowledge
        self.web = web or config.resolve_web_settings()
        self.settings = config.feishu_settings()
        self.app_id = self.settings["app_id"]
        self.app_secret = self.settings["app_secret"]
        self.accounts_base = self.settings["accounts_base"]
        self.api_base = self.settings["api_base"]
        self.scope = self.settings.get("scope", "")
        self.secret = self._load_secret()
        self._workspaces: Dict[str, FeishuUserWorkspace] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ 配置
    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def _load_secret(self) -> bytes:
        """签名密钥：配置/环境变量优先，否则生成并持久化，保证重启后登录态仍有效。"""
        configured = self.settings.get("session_secret") or os.getenv("DOC_AGENT_FEISHU_SECRET", "").strip()
        if configured:
            return configured.encode("utf-8")

        path = self.root / "session.secret"
        try:
            saved = path.read_text(encoding="utf-8").strip()
            if saved:
                return saved.encode("utf-8")
        except OSError:
            pass

        generated = secrets.token_urlsafe(48)
        try:
            path.write_text(generated, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            print("[飞书] 会话密钥无法写入磁盘，服务重启后用户需要重新登录")
        return generated.encode("utf-8")

    # ------------------------------------------------------------------ 签名 Cookie
    @staticmethod
    def _b64encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _b64decode(text: str) -> bytes:
        return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))

    def _signature(self, body: bytes) -> str:
        digest = hmac.new(self.secret, body, hashlib.sha256).digest()
        return self._b64encode(digest)

    def _pack(self, payload: Dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return f"{self._b64encode(body)}.{self._signature(body)}"

    def _unpack(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token or "." not in token:
            return None
        encoded, signature = token.rsplit(".", 1)
        try:
            body = self._b64decode(encoded)
        except Exception:
            return None
        if not hmac.compare_digest(signature, self._signature(body)):
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            expires = float(payload.get("exp") or 0)
        except (TypeError, ValueError):
            return None
        return payload if expires >= time.time() else None

    def create_session_cookie(self, open_id: str) -> str:
        return self._pack({"sub": open_id, "exp": int(time.time()) + SESSION_MAX_AGE})

    def verify_session_cookie(self, token: Optional[str]) -> Optional[str]:
        payload = self._unpack(token)
        if not payload:
            return None
        open_id = str(payload.get("sub") or "").strip()
        return open_id or None

    def pack_state(self, state: str, redirect_uri: str) -> str:
        return self._pack(
            {"state": state, "redirect_uri": redirect_uri, "exp": int(time.time()) + STATE_MAX_AGE}
        )

    def unpack_state(self, token: Optional[str]) -> Optional[Dict[str, str]]:
        payload = self._unpack(token)
        if not payload:
            return None
        state = str(payload.get("state") or "")
        redirect_uri = str(payload.get("redirect_uri") or "")
        if not state or not redirect_uri:
            return None
        return {"state": state, "redirect_uri": redirect_uri}

    @staticmethod
    def secure_cookie(request: Request) -> bool:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or forwarded == "https"

    # ------------------------------------------------------------------ 用户档案
    def _user_key(self, open_id: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", open_id):
            return open_id
        return hashlib.sha256(open_id.encode("utf-8")).hexdigest()[:24]

    def _user_dir(self, open_id: str, create: bool = True) -> Path:
        path = self.users_root / self._user_key(open_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def save_user(self, user: FeishuUser) -> None:
        directory = self._user_dir(user.open_id)
        payload = {**asdict(user), "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        target = directory / "profile.json"
        temporary = target.with_name(f"profile.{secrets.token_hex(4)}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def load_user(self, open_id: str) -> Optional[FeishuUser]:
        if not open_id:
            return None
        path = self._user_dir(open_id, create=False) / "profile.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        stored_id = str(data.get("open_id") or "").strip()
        if not stored_id or stored_id != open_id:
            return None
        return FeishuUser(
            open_id=stored_id,
            name=str(data.get("name") or "").strip() or "飞书用户",
            avatar_url=str(data.get("avatar_url") or ""),
            union_id=str(data.get("union_id") or ""),
            user_id=str(data.get("user_id") or ""),
            tenant_key=str(data.get("tenant_key") or ""),
        )

    def user_from_request(self, request: Request) -> Optional[FeishuUser]:
        open_id = self.verify_session_cookie(request.cookies.get(SESSION_COOKIE))
        return self.load_user(open_id) if open_id else None

    def workspace(self, user: FeishuUser) -> FeishuUserWorkspace:
        with self._guard:
            workspace = self._workspaces.get(user.open_id)
            if workspace is None:
                workspace = FeishuUserWorkspace(
                    self._user_dir(user.open_id) / "sessions",
                    self.knowledge,
                )
                self._workspaces[user.open_id] = workspace
            return workspace

    def recover_interrupted(self) -> int:
        """服务重启后，把所有飞书用户遗留的“运行中”任务标记为中断。"""
        recovered = 0
        for user_dir in self.users_root.iterdir():
            if not user_dir.is_dir():
                continue
            sessions_root = user_dir / "sessions"
            if not sessions_root.is_dir():
                continue
            try:
                recovered += SessionStore(sessions_root).recover_interrupted()
            except Exception as exc:
                print(f"[飞书] 恢复用户中断任务失败（{user_dir.name}）：{exc}")
        return recovered

    def close(self) -> None:
        with self._guard:
            workspaces = list(self._workspaces.values())
            self._workspaces.clear()
        for workspace in workspaces:
            for task in list(workspace.tasks):
                task.cancel()

    # ------------------------------------------------------------------ 飞书 OAuth
    def resolve_redirect_uri(self, request: Request) -> str:
        """回调地址：配置优先，否则按当前请求自动推导（反向代理场景建议显式配置）。"""
        if self.settings["redirect_uri"]:
            return self.settings["redirect_uri"]
        base = self.settings["base_url"] or str(request.base_url)
        return base.rstrip("/") + "/api/feishu/auth/callback"

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """构造飞书授权页地址（当前为 accounts.feishu.cn 的 authen v1 授权页）。"""
        params = {
            "client_id": self.app_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if self.scope:
            params["scope"] = self.scope
        return f"{self.accounts_base}/open-apis/authen/v1/authorize?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> str:
        """用授权码换 user_access_token（飞书 OAuth v3 令牌端点）。"""
        url = f"{self.accounts_base}/oauth/v3/token"
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        try:
            response = httpx.post(url, json=payload, timeout=15.0)
            data = response.json()
        except Exception as exc:  # 网络/解析错误都不把细节暴露给浏览器
            raise FeishuAuthError(f"请求飞书服务器失败：{exc}") from exc
        if not isinstance(data, dict):
            raise FeishuAuthError("飞书返回了无法解析的结果")

        body = data.get("data") if isinstance(data.get("data"), dict) else data
        token = str(body.get("access_token") or "").strip()
        if data.get("code") not in (0, None) or not token:
            message = (
                data.get("error_description")
                or data.get("msg")
                or body.get("error_description")
                or body.get("msg")
                or "获取飞书用户访问凭证失败"
            )
            raise FeishuAuthError(str(message))
        return token

    def fetch_user_info(self, access_token: str) -> FeishuUser:
        """读取飞书用户信息（名称、头像、open_id）。"""
        url = f"{self.api_base}/open-apis/authen/v1/user_info"
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15.0,
            )
            data = response.json()
        except Exception as exc:
            raise FeishuAuthError(f"获取飞书用户信息失败：{exc}") from exc
        if not isinstance(data, dict) or data.get("code") != 0:
            message = data.get("msg") if isinstance(data, dict) else "获取飞书用户信息失败"
            raise FeishuAuthError(str(message or "获取飞书用户信息失败"))

        info = data.get("data") if isinstance(data.get("data"), dict) else {}
        open_id = str(info.get("open_id") or "").strip()
        if not open_id:
            raise FeishuAuthError("飞书返回的用户信息缺少 open_id")
        name = str(info.get("name") or info.get("en_name") or "").strip() or "飞书用户"
        avatar = str(
            info.get("avatar_url")
            or info.get("avatar_big")
            or info.get("avatar_middle")
            or info.get("avatar_thumb")
            or ""
        )
        return FeishuUser(
            open_id=open_id,
            name=name,
            avatar_url=avatar,
            union_id=str(info.get("union_id") or ""),
            user_id=str(info.get("user_id") or ""),
            tenant_key=str(info.get("tenant_key") or ""),
        )


# --------------------------------------------------------------------------- 依赖
def get_service(request: Request) -> FeishuService:
    service = getattr(request.app.state, "feishu", None)
    if service is None:
        # 兜底：应用状态由 lifespan 注入，测试/异常场景下从全局状态获取
        from .app import backend_state

        service = backend_state().feishu
    return service


def current_user(
    request: Request,
    service: FeishuService = Depends(get_service),
) -> FeishuUser:
    user = service.user_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请使用飞书账号登录")
    return user


def current_workspace(
    user: FeishuUser = Depends(current_user),
    service: FeishuService = Depends(get_service),
) -> FeishuUserWorkspace:
    return service.workspace(user)


# --------------------------------------------------------------------------- 请求模型
class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionRename(BaseModel):
    title: str


class MessageCreate(BaseModel):
    content: str


# --------------------------------------------------------------------------- 登录
@router.get("/auth/status")
def auth_status(
    request: Request,
    service: FeishuService = Depends(get_service),
) -> JSONResponse:
    user = service.user_from_request(request)
    redirect_uri = service.resolve_redirect_uri(request)
    payload = {
        "configured": service.configured,
        "authenticated": user is not None,
        "user": user.public() if user else None,
        # 登录页会原样展示该地址，管理员据此在飞书后台登记重定向 URL
        "redirect_uri": redirect_uri,
        "redirect_uri_configured": bool(service.settings.get("redirect_uri")),
        "base_url": service.settings.get("base_url") or "",
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/auth/login")
def auth_login(
    request: Request,
    service: FeishuService = Depends(get_service),
) -> RedirectResponse:
    if not service.configured:
        return RedirectResponse(
            url="/feishu?error=unconfigured",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    state = secrets.token_urlsafe(24)
    redirect_uri = service.resolve_redirect_uri(request)
    response = RedirectResponse(
        service.authorize_url(state, redirect_uri),
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        STATE_COOKIE,
        service.pack_state(state, redirect_uri),
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=service.secure_cookie(request),
        path=STATE_COOKIE_PATH,
    )
    return response


@router.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    service: FeishuService = Depends(get_service),
) -> RedirectResponse:
    if not service.configured:
        return RedirectResponse(
            url="/feishu?error=unconfigured",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    # 用户在飞书授权页拒绝授权 / 飞书回调其它错误时，给出可区分的提示
    if error:
        description = (error_description or "").lower()
        if error == "access_denied":
            result = "access_denied"
        elif error in {"invalid_request", "invalid_redirect_uri"} or "redirect" in description:
            result = "redirect_uri_invalid"
        else:
            result = "login_failed"
        print(f"[飞书] 授权回调错误：error={error} description={error_description}")
        return RedirectResponse(
            url=f"/feishu?error={result}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    state_data = service.unpack_state(request.cookies.get(STATE_COOKIE))
    if not code or not state_data or state_data.get("state") != state:
        return RedirectResponse(
            url="/feishu?error=state_invalid",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    try:
        access_token = service.exchange_code(code, state_data["redirect_uri"])
        user = service.fetch_user_info(access_token)
    except FeishuAuthError as exc:
        print(f"[飞书] 登录失败：{exc}")
        return RedirectResponse(
            url="/feishu?error=login_failed",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    service.save_user(user)
    response = RedirectResponse(
        url="/feishu",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        SESSION_COOKIE,
        service.create_session_cookie(user.open_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=service.secure_cookie(request),
        path=SESSION_COOKIE_PATH,
    )
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return response


@router.get("/auth/logout")
def auth_logout() -> RedirectResponse:
    response = RedirectResponse(
        url="/feishu",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )
    response.delete_cookie(SESSION_COOKIE, path=SESSION_COOKIE_PATH)
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return response


# --------------------------------------------------------------------------- 健康检查
@router.get("/health")
def feishu_health(
    user: FeishuUser = Depends(current_user),
    workspace: FeishuUserWorkspace = Depends(current_workspace),
    service: FeishuService = Depends(get_service),
) -> Dict[str, Any]:
    data = workspace.runtime.health()
    web = service.web
    data.update(
        {
            "version": __version__,
            "session_count": len(workspace.store.list_sessions()),
            "user": user.public(),
            "feishu_configured": service.configured,
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


# --------------------------------------------------------------------------- 会话
@router.get("/sessions")
def list_sessions(workspace: FeishuUserWorkspace = Depends(current_workspace)) -> Dict[str, Any]:
    return {"sessions": workspace.store.list_sessions()}


@router.post("/sessions", status_code=201)
def create_session(
    payload: Optional[SessionCreate] = None,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Dict[str, Any]:
    return workspace.store.create(payload.title if payload else None)


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Dict[str, Any]:
    return workspace.store.detail(session_id)


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: str,
    payload: SessionRename,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Dict[str, Any]:
    return workspace.store.rename(session_id, payload.title)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Response:
    active = workspace.jobs.active_for_session(session_id)
    if active is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成文档，请等任务结束后再删除")
    workspace.runtime.forget(session_id)
    workspace.jobs.forget_session(session_id)
    workspace.store.delete(session_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- 文件
def _file_stream_size(upload: UploadFile) -> int:
    stream = upload.file
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    return size


@router.post("/sessions/{session_id}/files", status_code=201)
async def upload_files(
    session_id: str,
    files: List[UploadFile] = File(...),
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Dict[str, Any]:
    store = workspace.store
    store.session_dir(session_id)
    if not files:
        raise HTTPException(status_code=400, detail="没有收到任何文件")
    if len(files) > config.MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {config.MAX_UPLOAD_FILES} 个文件")

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
def download_file(
    session_id: str,
    name: str,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> FileResponse:
    path = workspace.store.resolve_file(session_id, name)
    return FileResponse(path, filename=path.name)


@router.delete("/sessions/{session_id}/files/{name:path}", status_code=204)
def delete_file(
    session_id: str,
    name: str,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Response:
    workspace.store.delete_file(session_id, name)
    return Response(status_code=204)


# --------------------------------------------------------------------------- 生成任务
async def _run_job(
    workspace: FeishuUserWorkspace,
    job: Job,
    session_id: str,
    prompt: str,
    assistant_message_id: str,
) -> None:
    try:
        await asyncio.to_thread(
            workspace.runtime.run_generation,
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
async def create_message(
    session_id: str,
    payload: MessageCreate,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Dict[str, Any]:
    store = workspace.store
    text = (payload.content or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="写作需求不能为空")
    store.session_dir(session_id)

    if workspace.jobs.active_for_session(session_id) is not None:
        raise HTTPException(status_code=409, detail="当前会话正在生成文档，请等待任务结束后再发送")
    if not workspace.runtime.try_begin(session_id):
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
        job = workspace.jobs.create(session_id)
        assistant_message = store.append_message(
            session_id,
            {"role": "assistant", "content": "", "status": "running", "job_id": job.id},
        )
    except Exception:
        try:
            workspace.runtime.session_lock(session_id).release()
        except RuntimeError:
            pass
        raise

    task = asyncio.create_task(
        _run_job(workspace, job, session_id, text, assistant_message["id"]),
        name=f"feishu-doc-agent-{job.id}",
    )
    workspace.tasks.add(task)
    task.add_done_callback(workspace.tasks.discard)

    return {
        "job_id": job.id,
        "message_id": assistant_message["id"],
        "user_message_id": user_message["id"],
        "session": store.meta(session_id),
    }


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> Dict[str, Any]:
    job = workspace.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启")
    return job.summary()


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str,
    request: Request,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> StreamingResponse:
    job = workspace.jobs.get(job_id)
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
def job_log(
    session_id: str,
    job_id: str,
    workspace: FeishuUserWorkspace = Depends(current_workspace),
) -> PlainTextResponse:
    path = workspace.store.log_path(session_id, job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="运行日志不存在")
    return PlainTextResponse(
        path.read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )
