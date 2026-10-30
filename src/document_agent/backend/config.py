"""Web 后端的路径与运行配置。

所有路径都基于源码位置推导，因此无论从哪个目录启动脚本，行为都一致；
需要改工作目录时通过环境变量 ``DOC_AGENT_WORKSPACE`` 指定。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Set

from .bootstrap import ensure_import_paths, src_dir

# 保证后续可以导入散装模块（intent_node 等）
ensure_import_paths()

BACKEND_DIR = Path(__file__).resolve().parent
SRC_DIR = src_dir()
PROJECT_ROOT = SRC_DIR.parent
STATIC_DIR = BACKEND_DIR / "static"
SETTING_PATH = SRC_DIR / "setting.json"

# 单个上传文件的大小上限（字节），避免误传超大文件写满磁盘
MAX_UPLOAD_BYTES = int(os.getenv("DOC_AGENT_MAX_UPLOAD_MB", "100")) * 1024 * 1024
# 一次请求最多上传的文件数量
MAX_UPLOAD_FILES = int(os.getenv("DOC_AGENT_MAX_UPLOAD_FILES", "20"))

DEFAULT_HOST = os.getenv("DOC_AGENT_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("DOC_AGENT_PORT", "8000"))

# 会话目录下的元数据目录（不出现在文件列表中）
META_DIR_NAME = ".meta"
SESSION_META_FILE = "session.json"
SESSION_MESSAGES_FILE = "messages.json"
LOG_DIR_NAME = "logs"

# 上传文件允许的后缀（与意图节点共用同一份轻量定义，避免在这里导入重型依赖）
_FALLBACK_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".json", ".csv", ".md"}

# 新建文档时必须的格式（改写目标只接受 docx）
DOCX_SUFFIX = ".docx"


def workspace_root() -> Path:
    """返回工作区根目录，默认 ``src/workspace``。"""
    raw = os.getenv("DOC_AGENT_WORKSPACE", "").strip()
    root = Path(raw).expanduser() if raw else SRC_DIR / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def sessions_root() -> Path:
    """返回会话根目录，每个会话是其中的一个子文件夹。"""
    root = workspace_root() / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


@lru_cache(maxsize=1)
def load_settings() -> Dict[str, Any]:
    """读取 ``src/setting.json``；缺失或格式错误时返回空字典（Web 版不因此退出）。"""
    try:
        with open(SETTING_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # 配置文件写错也不能让 Web 服务起不来
        print(f"[配置] 读取 {SETTING_PATH} 失败，改用默认配置：{exc}")
        return {}


@lru_cache(maxsize=1)
def supported_upload_suffixes() -> Set[str]:
    """上传/检索支持的文件后缀（轻量模块提供，不会触发智能体依赖的首次导入）。"""
    try:
        from document_agent.file_types import SUPPORTED_REFERENCE_SUFFIXES

        return {str(item).lower() for item in SUPPORTED_REFERENCE_SUFFIXES}
    except Exception:
        return set(_FALLBACK_SUFFIXES)


def allowed_suffixes_label() -> str:
    return "、".join(sorted(supported_upload_suffixes()))


def llm_api_key() -> str:
    return (os.getenv("MODEL_API_KEY") or "").strip()


def llm_base_url() -> str:
    return (os.getenv("MODEL_BASE_URL") or "https://api.deepseek.com/v1").strip()


def llm_model_name() -> str:
    return (os.getenv("MODEL_NAME") or "deepseek-chat").strip()


def llm_configured() -> bool:
    return bool(llm_api_key())


def cors_origins() -> Optional[list]:
    """可选的跨域白名单，默认不开放（前端与后端同源部署）。"""
    raw = os.getenv("DOC_AGENT_CORS_ORIGINS", "").strip()
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def server_url(host: str, port: int) -> str:
    """拼出浏览器可访问的地址（0.0.0.0 监听时换成 127.0.0.1）。"""
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{shown}:{port}/"
