"""会话文件夹的持久化。

每个会话对应 ``workspace/sessions/<session_id>/`` 一个真实目录：用户上传的参考文件、
智能体生成的文档都直接放在该目录下（方便用户在资源管理器里直接查看/取用），
元数据与消息记录放在同级的 ``.meta/`` 子目录中，不会污染文件列表。

目录结构：
    <session_id>/
        .meta/session.json      # 标题、时间、已生成文件清单
        .meta/messages.json     # 会话消息
        .meta/logs/<job>.log    # 每次生成任务的运行日志
        参考文件.docx / 生成文档.docx ...
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import config


class SessionError(Exception):
    """会话操作相关的业务异常。"""


class SessionNotFound(SessionError):
    pass


class InvalidFileName(SessionError):
    pass


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
# Windows 与 POSIX 都不适合出现在文件名里的字符，统一替换掉
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_filename(name: str, fallback: str = "上传文件") -> str:
    """把用户上传的文件名清洗成安全的文件名（去掉路径与非法字符）。"""
    raw = str(name or "").replace("\\", "/")
    base = Path(raw).name.strip()
    base = _ILLEGAL_NAME_CHARS.sub("_", base).strip().strip(".")
    if not base:
        base = fallback
    # 名字过长时保留后缀，截断主名
    if len(base) > 120:
        suffix = Path(base).suffix[:16]
        base = Path(base).stem[: 120 - len(suffix)] + suffix
    return base


def _format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class SessionStore:
    """会话文件的读写门面，所有路径都限制在会话目录内部。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 基础路径
    def session_dir(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(str(session_id or "")):
            raise SessionNotFound(f"会话不存在：{session_id}")
        path = self.root / session_id
        if not path.is_dir():
            raise SessionNotFound(f"会话不存在：{session_id}")
        return path

    def _meta_dir(self, session_id: str) -> Path:
        path = self.session_dir(session_id) / config.META_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _meta_path(self, session_id: str) -> Path:
        return self._meta_dir(session_id) / config.SESSION_META_FILE

    def _messages_path(self, session_id: str) -> Path:
        return self._meta_dir(session_id) / config.SESSION_MESSAGES_FILE

    def log_path(self, session_id: str, job_id: str) -> Path:
        safe_job = sanitize_filename(job_id, "job")
        path = self._meta_dir(session_id) / config.LOG_DIR_NAME / f"{safe_job}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ JSON 工具
    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------------ 会话生命周期
    def create(self, title: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            session_id = self._new_session_id()
            directory = self.root / session_id
            directory.mkdir(parents=True, exist_ok=False)
            now = _now_iso()
            meta = {
                "id": session_id,
                "title": (title or "").strip() or self._default_title(),
                "created_at": now,
                "updated_at": now,
                "generated": [],
            }
            self._meta_dir(session_id)
            self._write_json(self._meta_path(session_id), meta)
            self._write_json(self._messages_path(session_id), [])
        return self._session_payload(meta)

    def _new_session_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for _ in range(20):
            candidate = f"{stamp}-{secrets.token_hex(3)}"
            if not (self.root / candidate).exists():
                return candidate
        return f"{stamp}-{secrets.token_hex(6)}"

    @staticmethod
    def _default_title() -> str:
        return f"会话 {datetime.now().strftime('%m-%d %H:%M')}"

    def exists(self, session_id: str) -> bool:
        try:
            self.session_dir(session_id)
            return True
        except SessionNotFound:
            return False

    def meta(self, session_id: str) -> Dict[str, Any]:
        data = self._read_json(self._meta_path(session_id), None)
        if not isinstance(data, dict):
            raise SessionNotFound(f"会话元数据丢失：{session_id}")
        data.setdefault("id", session_id)
        data.setdefault("generated", [])
        return data

    def _touch(self, session_id: str, meta: Dict[str, Any]) -> None:
        meta["updated_at"] = _now_iso()
        self._write_json(self._meta_path(session_id), meta)

    def rename(self, session_id: str, title: str) -> Dict[str, Any]:
        clean = (title or "").strip()
        if not clean:
            raise SessionError("会话标题不能为空")
        with self._lock:
            meta = self.meta(session_id)
            meta["title"] = clean[:120]
            self._touch(session_id, meta)
        return self._session_payload(meta)

    def delete(self, session_id: str) -> None:
        with self._lock:
            directory = self.session_dir(session_id)
            shutil.rmtree(directory)

    def _session_payload(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": meta["id"],
            "title": meta.get("title") or meta["id"],
            "created_at": meta.get("created_at") or "",
            "updated_at": meta.get("updated_at") or "",
            "generated": list(meta.get("generated") or []),
            "path": str(self.root / meta["id"]),
        }

    # ------------------------------------------------------------------ 列表与详情
    def list_sessions(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not _SESSION_ID_RE.match(child.name):
                continue
            meta = self._read_json(child / config.META_DIR_NAME / config.SESSION_META_FILE, None)
            if not isinstance(meta, dict):
                continue
            meta.setdefault("id", child.name)
            payload = self._session_payload(meta)
            files = self.list_files(child.name)
            messages = self.list_messages(child.name)
            payload["file_count"] = len(files)
            payload["document_count"] = sum(1 for item in files if item["kind"] == "generated")
            payload["last_message"] = self._last_message_excerpt(messages)
            payload["running"] = any(m.get("status") == "running" for m in messages)
            items.append(payload)
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return items

    @staticmethod
    def _last_message_excerpt(messages: Iterable[Dict[str, Any]]) -> str:
        for message in reversed(list(messages)):
            content = str(message.get("content") or "").strip()
            if content:
                return content[:60] + ("…" if len(content) > 60 else "")
        return ""

    def detail(self, session_id: str) -> Dict[str, Any]:
        meta = self.meta(session_id)
        payload = self._session_payload(meta)
        payload["files"] = self.list_files(session_id)
        payload["messages"] = self.list_messages(session_id)
        return payload

    def recover_interrupted(self) -> int:
        """把上次进程遗留的「运行中」消息标记为中断（服务重启后任务已不可能继续）。"""
        recovered = 0
        for child in self.root.iterdir():
            if not child.is_dir() or not _SESSION_ID_RE.match(child.name):
                continue
            try:
                self.meta(child.name)  # 只处理结构完整的会话目录
            except SessionError:
                continue
            messages = self.list_messages(child.name)
            changed = False
            for message in messages:
                if message.get("role") == "assistant" and message.get("status") == "running":
                    message["status"] = "failed"
                    message["error"] = "服务重启导致任务中断，请重新发送需求"
                    message["updated_at"] = _now_iso()
                    changed = True
                    recovered += 1
            if not changed:
                continue
            with self._lock:
                self._write_json(self._messages_path(child.name), messages)
                self._touch(child.name, self.meta(child.name))
        return recovered

    # ------------------------------------------------------------------ 文件
    def list_files(self, session_id: str) -> List[Dict[str, Any]]:
        directory = self.session_dir(session_id)
        meta = self.meta(session_id)
        generated = {sanitize_filename(name, "x") for name in meta.get("generated") or []}
        items: List[Dict[str, Any]] = []
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if path.name.startswith(".") or path.suffix.lower() == ".tmp":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            suffix = path.suffix.lower()
            items.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "size_text": _format_size(stat.st_size),
                    "mtime": stat.st_mtime,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "suffix": suffix.lstrip("."),
                    "kind": "generated" if path.name in generated else "uploaded",
                    "is_docx": suffix == config.DOCX_SUFFIX,
                }
            )
        items.sort(key=lambda item: (0 if item["kind"] == "generated" else 1, -item["mtime"]))
        return items

    def file_paths(self, session_id: str, only_supported: bool = True) -> List[str]:
        """返回会话目录下可作为参考/改写目标的文件绝对路径。"""
        allowed = config.supported_upload_suffixes() if only_supported else None
        directory = self.session_dir(session_id)
        paths: List[str] = []
        for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file():
                continue
            if path.name.startswith(".") or path.suffix.lower() == ".tmp":
                continue
            if allowed is not None and path.suffix.lower() not in allowed:
                continue
            paths.append(str(path.resolve()))
        return paths

    def unique_name(self, session_id: str, filename: str) -> str:
        """在会话目录内生成不冲突的文件名，重名时追加 _1、_2。"""
        base = sanitize_filename(filename)
        directory = self.session_dir(session_id)
        if not (directory / base).exists():
            return base
        stem = Path(base).stem
        suffix = Path(base).suffix
        index = 1
        while (directory / f"{stem}_{index}{suffix}").exists():
            index += 1
        return f"{stem}_{index}{suffix}"

    def save_upload(self, session_id: str, filename: str, source) -> Dict[str, Any]:
        """把上传的二进制流写入会话目录，返回文件信息。"""
        clean = sanitize_filename(filename)
        suffix = Path(clean).suffix.lower()
        if suffix not in config.supported_upload_suffixes():
            raise InvalidFileName(
                f"不支持的文件类型：{clean}（支持 {config.allowed_suffixes_label()}）"
            )
        with self._lock:
            directory = self.session_dir(session_id)
            final_name = self.unique_name(session_id, clean)
            target = directory / final_name
            tmp = directory / f".{final_name}.{secrets.token_hex(4)}.tmp"
            try:
                with open(tmp, "wb") as fp:
                    shutil.copyfileobj(source, fp, length=1024 * 1024)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            meta = self.meta(session_id)
            self._touch(session_id, meta)
        return self._file_payload(target)

    def _file_payload(self, path: Path) -> Dict[str, Any]:
        stat = path.stat()
        suffix = path.suffix.lower()
        return {
            "name": path.name,
            "size": stat.st_size,
            "size_text": _format_size(stat.st_size),
            "mtime": stat.st_mtime,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "suffix": suffix.lstrip("."),
            "kind": "uploaded",
            "is_docx": suffix == config.DOCX_SUFFIX,
        }

    def resolve_file(self, session_id: str, name: str) -> Path:
        """把请求里的文件名解析为会话目录内的真实文件，拒绝越界路径。"""
        directory = self.session_dir(session_id)
        clean = Path(str(name).replace("\\", "/")).name
        if not clean or clean in (".", ".."):
            raise InvalidFileName("文件名不合法")
        target = (directory / clean).resolve()
        try:
            target.relative_to(directory.resolve())
        except ValueError:
            raise InvalidFileName("文件名不合法") from None
        if not target.is_file():
            raise InvalidFileName(f"文件不存在：{clean}")
        return target

    def delete_file(self, session_id: str, name: str) -> None:
        target = self.resolve_file(session_id, name)
        with self._lock:
            target.unlink()
            meta = self.meta(session_id)
            generated = [item for item in meta.get("generated") or [] if item != target.name]
            meta["generated"] = generated
            self._touch(session_id, meta)

    def mark_generated(self, session_id: str, names: Iterable[str]) -> None:
        with self._lock:
            meta = self.meta(session_id)
            generated = list(meta.get("generated") or [])
            for name in names:
                if name and name not in generated:
                    generated.append(name)
            meta["generated"] = generated
            self._touch(session_id, meta)

    # ------------------------------------------------------------------ 消息
    def list_messages(self, session_id: str) -> List[Dict[str, Any]]:
        data = self._read_json(self._messages_path(session_id), [])
        return data if isinstance(data, list) else []

    def append_message(self, session_id: str, message: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            messages = self.list_messages(session_id)
            record = {
                "id": secrets.token_hex(8),
                "created_at": _now_iso(),
                **message,
            }
            messages.append(record)
            self._write_json(self._messages_path(session_id), messages)
            meta = self.meta(session_id)
            self._touch(session_id, meta)
        return record

    def update_message(self, session_id: str, message_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            messages = self.list_messages(session_id)
            updated: Optional[Dict[str, Any]] = None
            for item in messages:
                if item.get("id") == message_id:
                    item.update(fields)
                    item["updated_at"] = _now_iso()
                    updated = item
                    break
            if updated is not None:
                self._write_json(self._messages_path(session_id), messages)
                meta = self.meta(session_id)
                self._touch(session_id, meta)
            return updated

    # ------------------------------------------------------------------ 日志
    def save_job_log(self, session_id: str, job_id: str, lines: Iterable[str]) -> str:
        try:
            path = self.log_path(session_id, job_id)
            with open(path, "w", encoding="utf-8") as fp:
                for line in lines:
                    fp.write(f"{line}\n")
            return path.name
        except Exception as exc:  # 日志写失败不能影响任务结果
            print(f"[日志] 保存运行日志失败：{exc}")
            return ""


__all__ = [
    "SessionStore",
    "SessionError",
    "SessionNotFound",
    "InvalidFileName",
    "sanitize_filename",
]
