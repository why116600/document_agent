"""远程知识图谱模式配置：决定检索阶段用「远程知识图谱」还是「本地图数据库」。

这个开关同时影响三处，因此集中在这里解析，保证控制台、Web 后端、智能体图读到的是
同一份配置：

1. ``AgentCore.build_graph``：远程模式装 ``kg_retrieve_node``，本地模式装 ``gdb_retrieve_node``；
2. ``console_app`` / ``backend.agent_runtime``：远程模式跳过 Memgraph 与本地知识图谱的初始化；
3. Web ``/api/health`` 与前端：显示当前模式，以及远程知识图谱的连通性。

配置写法（``src/setting.json``，推荐嵌套写法）::

    "remote_kg": {
        "enabled": true,
        "base_url": "http://192.168.1.136:8000",
        "auth_token": "",
        "timeout": 60,
        "max_retries": 2
    }

也兼容扁平写法：``remote_kg_enabled`` / ``remote_kg_url`` / ``remote_kg_token`` /
``remote_kg_timeout``。

优先级（高 → 低）：环境变量 → ``setting.json`` → 默认值（关闭、``http://127.0.0.1:8000``）。
环境变量：``DOC_AGENT_REMOTE_KG``（开关）、``DOC_AGENT_REMOTE_KG_URL`` 或
``REMOTE_KG_BASE_URL``（地址）、``REMOTE_KG_API_TOKEN`` 或 ``API_TOKEN``（鉴权）、
``DOC_AGENT_REMOTE_KG_TIMEOUT``（超时秒数）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

SETTING_FILE_NAME = "setting.json"
DEFAULT_REMOTE_KG_URL = "http://127.0.0.1:8000"
DEFAULT_REMOTE_KG_TIMEOUT = 60.0
DEFAULT_REMOTE_KG_RETRIES = 2

_TRUE_WORDS = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSE_WORDS = {"0", "false", "no", "off", "disable", "disabled"}


def as_bool(value: Any, default: bool = False) -> bool:
    """把配置文件/环境变量里的各种写法解析成布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def find_setting_path(explicit: Optional[str] = None) -> Path:
    """定位配置文件：显式路径 → ``DOC_AGENT_SETTING`` → 包内 ``src/setting.json`` → 当前目录。"""
    if explicit:
        return Path(explicit).expanduser()
    env_path = (os.getenv("DOC_AGENT_SETTING") or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    package_src = Path(__file__).resolve().parents[2]
    for candidate in (package_src / SETTING_FILE_NAME, Path.cwd() / SETTING_FILE_NAME):
        if candidate.is_file():
            return candidate
    return package_src / SETTING_FILE_NAME


def read_settings(config_path: Optional[str] = None) -> Tuple[Dict[str, Any], Path]:
    """读取配置文件；缺失或格式错误时返回空字典（不抛异常，由调用方决定如何降级）。"""
    path = find_setting_path(config_path)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return (data if isinstance(data, dict) else {}), path
    except FileNotFoundError:
        return {}, path
    except Exception as exc:  # 配置写错不能拖垮启动
        print(f"[配置] 读取 {path} 失败，远程知识图谱按默认值处理：{exc}")
        return {}, path


@dataclass(frozen=True)
class RemoteKGSettings:
    """远程知识图谱的运行配置快照。"""

    enabled: bool = False
    base_url: str = DEFAULT_REMOTE_KG_URL
    auth_token: str = ""
    timeout: float = DEFAULT_REMOTE_KG_TIMEOUT
    max_retries: int = DEFAULT_REMOTE_KG_RETRIES
    config_path: str = ""
    enabled_source: str = "默认关闭"
    url_source: str = "默认值"

    @property
    def mode(self) -> str:
        """``remote`` 或 ``local``，供日志与前端直接展示。"""
        return "remote" if self.enabled else "local"

    def tool_kwargs(self) -> Dict[str, Any]:
        """构造 :class:`RemoteKGTool` 时用的参数（token 为空时交给环境变量兜底）。"""
        return {
            "auth_token": self.auth_token or None,
            "timeout": max(1.0, self.timeout),
            "max_retries": max(0, self.max_retries),
        }

    def public(self) -> Dict[str, Any]:
        """可安全返回给前端的配置摘要（不含 token 明文）。"""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "url": self.base_url,
            "auth_enabled": bool(self.auth_token),
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "config_path": self.config_path,
            "enabled_source": self.enabled_source,
            "url_source": self.url_source,
        }

    def describe(self) -> str:
        """一行日志：``远程知识图谱模式：http://…（来源：setting.json）``。"""
        if not self.enabled:
            return f"本地知识图谱模式（本地图数据库；开关来源：{self.enabled_source}）"
        return f"远程知识图谱模式：{self.base_url}（地址来源：{self.url_source}）"


def load_remote_kg_settings(
    config_path: Optional[str] = None,
    settings: Optional[Mapping[str, Any]] = None,
) -> RemoteKGSettings:
    """解析远程知识图谱配置。

    :param config_path: 显式指定配置文件；``None`` 时按 :func:`find_setting_path` 查找
    :param settings: 已经解析好的配置字典；传入时不再读文件（Web 后端复用同一份）
    """
    if settings is None:
        data, path = read_settings(config_path)
    else:
        data = dict(settings)
        path = find_setting_path(config_path)

    block = data.get("remote_kg")
    block = block if isinstance(block, Mapping) else {}

    # ---- 开关 ----
    if "enabled" in block:
        enabled = as_bool(block.get("enabled"), False)
        enabled_source = f"{path.name}: remote_kg.enabled"
    elif "remote_kg_enabled" in data:
        enabled = as_bool(data.get("remote_kg_enabled"), False)
        enabled_source = f"{path.name}: remote_kg_enabled"
    else:
        enabled = False
        enabled_source = "默认关闭（未配置）"
    env_enabled = (os.getenv("DOC_AGENT_REMOTE_KG") or "").strip()
    if env_enabled:
        enabled = as_bool(env_enabled, enabled)
        enabled_source = "环境变量 DOC_AGENT_REMOTE_KG"

    # ---- 地址 ----
    url = str(block.get("base_url") or block.get("url") or "").strip()
    if not url:
        url = str(data.get("remote_kg_url") or "").strip()
    url_source = path.name if url else "默认值"
    for env_name in ("DOC_AGENT_REMOTE_KG_URL", "REMOTE_KG_BASE_URL"):
        env_url = (os.getenv(env_name) or "").strip()
        if env_url:
            url, url_source = env_url, f"环境变量 {env_name}"
            break
    url = (url or DEFAULT_REMOTE_KG_URL).rstrip("/")

    # ---- 鉴权 ----
    token = str(block.get("auth_token") or block.get("token") or "").strip()
    if not token:
        token = str(data.get("remote_kg_token") or "").strip()
    if not token:
        token = (os.getenv("REMOTE_KG_API_TOKEN") or os.getenv("API_TOKEN") or "").strip()

    # ---- 超时与重试 ----
    timeout = _as_float(
        block.get("timeout", data.get("remote_kg_timeout")), DEFAULT_REMOTE_KG_TIMEOUT
    )
    env_timeout = (os.getenv("DOC_AGENT_REMOTE_KG_TIMEOUT") or "").strip()
    if env_timeout:
        timeout = _as_float(env_timeout, timeout)
    max_retries = _as_int(block.get("max_retries"), DEFAULT_REMOTE_KG_RETRIES)

    return RemoteKGSettings(
        enabled=enabled,
        base_url=url,
        auth_token=token,
        timeout=timeout,
        max_retries=max_retries,
        config_path=str(path),
        enabled_source=enabled_source,
        url_source=url_source,
    )


__all__ = [
    "DEFAULT_REMOTE_KG_URL",
    "DEFAULT_REMOTE_KG_TIMEOUT",
    "DEFAULT_REMOTE_KG_RETRIES",
    "RemoteKGSettings",
    "as_bool",
    "find_setting_path",
    "read_settings",
    "load_remote_kg_settings",
]
