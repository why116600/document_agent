"""Web 后端的路径与运行配置。

所有路径都基于源码位置推导，因此无论从哪个目录启动脚本，行为都一致；
需要改工作目录时通过环境变量 ``DOC_AGENT_WORKSPACE`` 指定。
"""

from __future__ import annotations

import json
import os
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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

# 监听地址：默认是否允许远程访问由 setting.json 的 web_allow_remote 决定，
# 命令行 --host 可以显式覆盖（覆盖时优先使用命令行）。
DEFAULT_LOCAL_HOST = "127.0.0.1"
REMOTE_BIND_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

# 命令行 --port / --host 的临时覆盖（仅进程内有效）
_port_override: Optional[int] = None
_host_override: Optional[str] = None

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


@lru_cache(maxsize=1)
def remote_kg_settings():
    """远程知识图谱配置（``setting.json`` 的 ``remote_kg`` 段，环境变量可覆盖）。

    返回 ``RemoteKGSettings``；其 ``enabled`` 决定检索阶段用远程知识图谱还是本地图数据库。
    """
    from remote_kg_config import load_remote_kg_settings

    return load_remote_kg_settings(settings=load_settings(), config_path=str(SETTING_PATH))


def knowledge_mode() -> str:
    """当前知识图谱模式：``remote`` 或 ``local``。"""
    return remote_kg_settings().mode


def cors_origins() -> Optional[list]:
    """可选的跨域白名单，默认不开放（前端与后端同源部署）。"""
    raw = os.getenv("DOC_AGENT_CORS_ORIGINS", "").strip()
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- 监听配置
def _as_bool(value: Any, default: bool = False) -> bool:
    """把配置里的布尔值（true/"true"/1/"on" 等）统一解析为 bool。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _as_port(value: Any) -> Optional[int]:
    """把配置里的端口解析为 1~65535 的整数，非法值返回 None。"""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def admin_settings() -> Dict[str, Any]:
    """Skills 管理入口配置；默认启用，但只接受本机访问。"""
    settings = load_settings()
    raw = settings.get("admin")
    if not isinstance(raw, dict):
        raw = {}
    return {"enabled": _as_bool(raw.get("enabled"), True)}


def set_port_override(port: Optional[int]) -> None:
    """记录命令行传入的端口（若合法），供应用启动时使用。"""
    global _port_override
    _port_override = _as_port(port) if port is not None else None


def set_host_override(host: Optional[str]) -> None:
    """记录命令行传入的监听地址；传空值时表示不覆盖，按配置文件处理。"""
    global _host_override
    text = str(host or "").strip()
    _host_override = text or None


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_wildcard(host: str) -> bool:
    return host in {"0.0.0.0", "::", ""}


def _local_ipv4_addresses() -> List[str]:
    """探测本机对外 IPv4（用 UDP 路由，不实际发包，也不做 DNS 查询）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return []
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return []
    return [ip]


def resolve_web_settings(
    port_override: Optional[int] = None,
    host_override: Optional[str] = None,
) -> Dict[str, Any]:
    """解析 Web 服务的监听配置。

    监听地址优先级：
      1. 命令行 ``--host``（脚本的 ``-BindHost`` / ``--host``）显式指定；
      2. 未指定时按 ``setting.json`` 的 ``web_allow_remote``：
         - ``false``（默认）：监听 127.0.0.1，仅本机可访问；
         - ``true``：监听 0.0.0.0，局域网内其他主机可通过本机 IP 访问。

    端口优先级：命令行 ``--port`` > setting.json 的 ``web_port`` >
    环境变量 ``DOC_AGENT_PORT`` > 8000。
    """
    settings = load_settings()
    configured_remote = _as_bool(settings.get("web_allow_remote"), False)

    override = host_override if host_override is not None else _host_override
    override = str(override or "").strip()
    if override:
        host = override
        host_source = "command-line"
    else:
        host = REMOTE_BIND_HOST if configured_remote else DEFAULT_LOCAL_HOST
        host_source = "config"

    # 监听地址不是回环地址时，其他主机就能访问（0.0.0.0 或具体的局域网 IP）
    allow_remote = not _is_loopback(host)
    # 浏览器/脚本探活使用的地址：通配地址用 127.0.0.1，具体地址保持不变
    visit_host = DEFAULT_LOCAL_HOST if _is_wildcard(host) else host

    if port_override is None:
        port_override = _port_override
    port = _as_port(port_override)
    if port is None:
        port = _as_port(settings.get("web_port"))
    if port is None:
        port = _as_port(os.getenv("DOC_AGENT_PORT"))
    if port is None:
        port = DEFAULT_PORT

    if allow_remote and _is_wildcard(host):
        remote_urls = [f"http://{ip}:{port}/" for ip in _local_ipv4_addresses()]
    elif allow_remote:
        remote_urls = [f"http://{host}:{port}/"]
    else:
        remote_urls = []

    return {
        "allow_remote": allow_remote,
        "configured_remote": configured_remote,
        "host": host,
        "host_source": host_source,
        "port": port,
        "local_url": f"http://{visit_host}:{port}/",
        "remote_urls": remote_urls,
        "config_path": str(SETTING_PATH),
        "config_found": SETTING_PATH.is_file(),
    }
