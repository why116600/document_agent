"""远程知识图谱检索协议（Retrieval Protocol）的异步通信客户端。

本模块按 ``retrieve_protocol.md`` 实现全部只读 HTTP 接口，供智能体/程序二次加工图谱原料：

* 工作状态 ``GET /retrieval/status`` → :meth:`RemoteKGTool.get_status`
* Schema 元信息 ``GET /retrieval/schema`` → :meth:`RemoteKGTool.get_schema`
* 关键词检索 ``GET|POST /retrieval/keywords`` → :meth:`RemoteKGTool.keyword_search`
* 语义检索 ``POST /retrieval/semantic`` → :meth:`RemoteKGTool.semantic_search`
* 图遍历 ``POST /retrieval/traverse`` → :meth:`RemoteKGTool.traverse`
* 路径查找 ``POST /retrieval/paths`` → :meth:`RemoteKGTool.find_paths`
* 读单个节点正文 ``GET /retrieval/nodes/{id}`` → :meth:`RemoteKGTool.get_node`
* 批量读节点正文 ``POST /retrieval/nodes/bodies`` → :meth:`RemoteKGTool.get_nodes_bodies`
* 实体识别与消歧 ``POST /retrieval/entities/resolve`` → :meth:`RemoteKGTool.resolve_entities`

设计要点
--------
* **全异步**：所有需要通信的接口都是 ``async def``，底层基于 ``httpx.AsyncClient``，
  同一实例可在同一事件循环内并发调用（协议 §13：检索接口可并发）。
* **失败可见**：HTTP 状态码映射为 :class:`RemoteKGError` 及其子类，异常上保留
  ``status_code`` / ``detail`` / ``url`` / ``payload``，调用方无需解析错误字符串。
* **提前校验**：按协议 §2.4 的规模上限在客户端先校验参数，越界直接抛 ``ValueError``，
  不浪费一次网络往返；返回值仍是服务端原始 JSON（``snake_case``、``score`` 仅在单次
  响应内可比）。
* **幂等重试**：整组接口只读（协议 §13），因此网络错误、超时与 5xx 可安全重试。
* **鉴权**：支持 ``X-API-Token`` 与 ``Authorization: Bearer`` 两种请求头（协议 §2.1）。
* **生命周期**：客户端惰性创建，``async with`` 或手动 ``await aclose()`` 释放连接池。

环境变量
--------
* ``REMOTE_KG_BASE_URL``：默认服务地址（缺省 ``http://127.0.0.1:8000``）。
* ``REMOTE_KG_API_TOKEN``：鉴权 token；未设置时回退 ``API_TOKEN``。

用法示例
--------
.. code-block:: python

    import asyncio
    from document_agent.retrieve_tool.remote_kg_tool import RemoteKGTool

    async def main():
        async with RemoteKGTool("http://127.0.0.1:8000") as kg:
            # 1. 调用前自检：三项全绿再继续
            status = await kg.get_status(probe_llm=False)
            if not status["ok"]:
                raise RuntimeError(f"服务不可用：{status['components']}")

            # 2. 不确定用词时先按语义找实体
            hits = await kg.semantic_search("谁创办了这家公司", entity_limit=5)
            for entity in hits["entities"]:
                # 3. 从命中实体取 1~2 跳子图
                subgraph = await kg.traverse(entity["id"], depth=2)
                # 4. 读回证据原文
                for node in subgraph["nodes"]:
                    if node["kind"] == "chunk":
                        body = await kg.get_node(node["id"], max_chars=1500)
                        print(body["body"]["text"][:200])

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Mapping, Optional, Sequence, Union
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "RemoteKGTool",
    "RemoteKGError",
    "RemoteKGConnectionError",
    "RemoteKGTimeoutError",
    "RemoteKGClientError",
    "RemoteKGBadRequestError",
    "RemoteKGAuthError",
    "RemoteKGNotFoundError",
    "RemoteKGValidationError",
    "RemoteKGServerError",
    "RemoteKGUpstreamError",
    "RemoteKGUnavailableError",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 60.0

_VERSION = "0.1.0"
_USER_AGENT = f"document-agent-remote-kg/{_VERSION}"

# ---- 协议 §2.4 服务端强制的规模上限，客户端提前校验 ----
_LIMIT_KEYWORD = (1, 50)  # 关键词检索每个通道的条数
_LIMIT_SEMANTIC = (1, 50)  # 语义检索实体/关系条数
_LIMIT_DEPTH = (1, 4)  # 遍历跳数
_LIMIT_NODE = (1, 500)  # 遍历节点上限
_LIMIT_EDGE = (1, 2000)  # 遍历边上限
_LIMIT_PATH_DEPTH = (1, 8)  # 路径最大跳数
_LIMIT_PATH_COUNT = (1, 10)  # 路径条数
_LIMIT_MAX_CHARS = (200, 40000)  # 正文字符上限
_LIMIT_BATCH_IDS = (1, 50)  # 批量正文的节点数
_LIMIT_TOP_K = (1, 10)  # 实体消歧候选数
_RESOLVE_TEXT_MAX = 20000  # 实体识别的输入长度上限

_KINDS = frozenset({"entity", "chunk", "document"})
_START_KINDS = frozenset({"auto", "entity", "document", "chunk"})
_DIRECTIONS = frozenset({"out", "in", "both"})
_AUTH_SCHEMES = frozenset({"x-api-token", "bearer", "both"})

# 只读接口遇到这些状态码可安全重试
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# 异常层次：把协议 §2.3 的错误码变成可判别的异常类型
# ---------------------------------------------------------------------------


class RemoteKGError(RuntimeError):
    """远程知识图谱接口异常基类。

    所有异常都带上足够定位问题的上下文：

    :ivar status_code: HTTP 状态码；纯网络故障（未拿到响应）时为 ``None``
    :ivar detail: 服务端 ``{"detail": ...}`` 的正文（已转成字符串）；无响应时为 ``None``
    :ivar url: 实际请求的 URL
    :ivar payload: 服务端返回的原始 JSON（解析失败时为原始文本）
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
        url: Optional[str] = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.url = url
        self.payload = payload

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else self.__class__.__name__


class RemoteKGConnectionError(RemoteKGError):
    """连接失败：服务没起、地址写错、网络不可达。"""


class RemoteKGTimeoutError(RemoteKGConnectionError):
    """请求超时。服务端在跑但响应太慢时触发（遍历/路径参数过大时常见，见协议 §13）。"""


class RemoteKGClientError(RemoteKGError):
    """4xx：请求本身有问题（参数、路径、鉴权），重试无意义。"""


class RemoteKGBadRequestError(RemoteKGClientError):
    """400：参数不合法 / 路径类错误（如起止节点都为空）。"""


class RemoteKGAuthError(RemoteKGClientError):
    """401：配置了 ``api.auth_token`` 但请求没带或带错 token（协议 §2.1）。"""


class RemoteKGNotFoundError(RemoteKGClientError):
    """404：节点或端点不存在（``traverse``/``paths`` 的端点、``nodes/{id}``）。"""


class RemoteKGValidationError(RemoteKGClientError):
    """422：请求体校验失败（字段缺失、``depth`` 超出 1~4、``max_chars`` 超出 200~40000 等）。"""


class RemoteKGServerError(RemoteKGError):
    """5xx：服务端内部错误。"""


class RemoteKGUpstreamError(RemoteKGServerError):
    """502：上游大模型失败（仅出现在需要 LLM 的支路，如实体识别的 ``use_llm=true``）。"""


class RemoteKGUnavailableError(RemoteKGServerError):
    """503：Neo4j 不可用（数据库没起或密码不对）。"""


_STATUS_ERRORS: dict[int, type[RemoteKGError]] = {
    400: RemoteKGBadRequestError,
    401: RemoteKGAuthError,
    404: RemoteKGNotFoundError,
    422: RemoteKGValidationError,
    502: RemoteKGUpstreamError,
    503: RemoteKGUnavailableError,
}


# ---------------------------------------------------------------------------
# 参数校验与序列化辅助
# ---------------------------------------------------------------------------


def _require_text(name: str, value: Any) -> str:
    """校验必填字符串参数（去首尾空白后非空）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"参数 {name} 必须是非空字符串，收到：{value!r}")
    return value.strip()


def _bounded_int(name: str, value: Any, bounds: tuple[int, int]) -> int:
    """校验整数参数并检查协议规定的取值范围。"""
    low, high = bounds
    if isinstance(value, bool):
        raise ValueError(f"参数 {name} 必须是整数，收到布尔值：{value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"参数 {name} 必须是整数，收到：{value!r}")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError(f"参数 {name} 必须是整数，收到：{value!r}")
    if not low <= value <= high:
        raise ValueError(f"参数 {name} 超出协议允许范围：{value} 不在 {low}~{high} 之间")
    return value


def _bounded_float(name: str, value: Any, bounds: tuple[float, float]) -> float:
    """校验浮点参数（如 ``score``/``min_score`` 约定在 0~1）。"""
    low, high = bounds
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"参数 {name} 必须是数字，收到：{value!r}")
    result = float(value)
    if not low <= result <= high:
        raise ValueError(f"参数 {name} 超出协议允许范围：{result} 不在 {low}~{high} 之间")
    return result


def _choice(name: str, value: Any, choices: frozenset[str]) -> str:
    """校验枚举参数。"""
    if value not in choices:
        raise ValueError(f"参数 {name} 只能是 {sorted(choices)} 之一，收到：{value!r}")
    return str(value)


def _text_list(
    name: str,
    values: Optional[Sequence[str]],
    *,
    allowed: Optional[frozenset[str]] = None,
) -> list[str]:
    """校验字符串列表参数：容错单值、按序去重、可限定枚举。"""
    if values is None:
        return []
    if isinstance(values, str):  # 容错：单值自动包装成列表
        values = [values]
    result: list[str] = []
    for item in values:
        text = _require_text(name, item)
        if allowed is not None and text not in allowed:
            raise ValueError(f"参数 {name} 的取值只能是 {sorted(allowed)} 之一，收到：{text!r}")
        if text not in result:
            result.append(text)
    return result


def _param_text(value: Any) -> str:
    """把 query 参数值序列化成服务端好解析的文本（布尔统一小写）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_params(params: Any) -> Optional[list[tuple[str, str]]]:
    """把参数字典/元组序列编码成可重复的查询串列表（协议 §2.2：列表参数可重复传）。"""
    if params is None:
        return None
    items = params.items() if isinstance(params, Mapping) else params
    encoded: list[tuple[str, str]] = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if item is not None:
                    encoded.append((str(key), _param_text(item)))
        else:
            encoded.append((str(key), _param_text(value)))
    return encoded or None


def _quote_node_id(name: str, value: Any) -> str:
    """把节点 ID 编码进路径段（关系 ID 形如 ``a|type|b``，必须转义）。"""
    text = _require_text(name, value)
    return quote(text, safe="")


def _extract_detail(payload: Any) -> Optional[str]:
    """取出 ``{"detail": ...}`` 并压平成字符串（FastAPI 422 的 detail 是列表）。"""
    if isinstance(payload, dict):
        detail = payload.get("detail")
    elif payload is None:
        return None
    else:
        return str(payload)
    if detail is None:
        return None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts: list[str] = []
        for item in detail:
            if isinstance(item, dict):
                location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
                message = str(item.get("msg", "")).strip()
                parts.append(f"{location} {message}".strip())
            else:
                parts.append(str(item))
        return "；".join(part for part in parts if part)
    try:
        return json.dumps(detail, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(detail)


def _decode_json(response: httpx.Response) -> Any:
    """解析响应体；空响应体按 ``{}`` 处理，非 JSON 直接报错。"""
    text = response.text
    if not text.strip():
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise RemoteKGError(
            f"响应不是合法 JSON（HTTP {response.status_code}）：{text[:200]}",
            status_code=response.status_code,
            url=str(response.request.url) if response.request is not None else None,
            payload=text[:2000],
        ) from exc


def _error_from_response(response: httpx.Response, method: str, path: str) -> RemoteKGError:
    """把错误响应翻译成对应异常类型。"""
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    detail = _extract_detail(payload)
    status = response.status_code
    message = f"{method} {path} 失败（HTTP {status}）：{detail or response.reason_phrase}"
    error_type = _STATUS_ERRORS.get(status)
    if error_type is None:
        error_type = RemoteKGServerError if status >= 500 else RemoteKGClientError
    return error_type(
        message,
        status_code=status,
        detail=detail,
        url=str(response.request.url) if response.request is not None else None,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class RemoteKGTool:
    """远程知识图谱检索协议的异步通信类。

    一个实例对应一个服务地址，内部持有一个 ``httpx.AsyncClient`` 连接池；同一实例
    可被多个协程并发使用。所有通信接口都是协程，必须 ``await``。

    :param base_url: 服务地址，默认读 ``REMOTE_KG_BASE_URL``，再回退
        ``http://127.0.0.1:8000``（协议 §2.1）
    :param auth_token: 鉴权 token；``None`` 时读 ``REMOTE_KG_API_TOKEN`` / ``API_TOKEN``，
        空字符串表示明确不鉴权
    :param auth_scheme: 鉴权头方式：``x-api-token``（默认，``X-API-Token``）、
        ``bearer``（``Authorization: Bearer ...``）或 ``both``（两个都发，服务端任选其一）
    :param timeout: 请求超时秒数（或 ``httpx.Timeout``）；默认 60 秒
    :param max_retries: 只读请求的最大重试次数（不含首次），默认 2 次
    :param retry_backoff: 指数退避的起始间隔秒数，默认 0.5 秒
    :param headers: 追加到每个请求上的自定义请求头
    :param transport: 自定义 ``httpx`` 传输层（测试时可注入 ``MockTransport``）

    :raises ValueError: ``base_url`` 为空、``auth_scheme`` 非法、``timeout``/重试参数不合法
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        auth_token: Optional[str] = None,
        auth_scheme: str = "x-api-token",
        timeout: Union[float, httpx.Timeout] = DEFAULT_TIMEOUT,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        headers: Optional[Mapping[str, str]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        resolved_base = base_url if base_url is not None else os.getenv("REMOTE_KG_BASE_URL")
        resolved_base = (resolved_base or DEFAULT_BASE_URL).strip()
        if not resolved_base:
            raise ValueError("base_url 不能为空")
        self.base_url = resolved_base.rstrip("/")

        if auth_token is None:
            auth_token = os.getenv("REMOTE_KG_API_TOKEN") or os.getenv("API_TOKEN") or ""
        self._auth_token = auth_token.strip()
        self._auth_scheme = _choice("auth_scheme", auth_scheme.strip().lower(), _AUTH_SCHEMES)

        if isinstance(timeout, (int, float)):
            if timeout <= 0:
                raise ValueError(f"timeout 必须大于 0，收到：{timeout!r}")
            timeout = float(timeout)
        self._timeout = timeout

        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError(f"max_retries 必须是非负整数，收到：{max_retries!r}")
        self._max_retries = max_retries
        if retry_backoff < 0:
            raise ValueError(f"retry_backoff 不能为负数，收到：{retry_backoff!r}")
        self._retry_backoff = float(retry_backoff)

        self._extra_headers = dict(headers or {})
        self._transport = transport

        # 连接池按事件循环惰性创建：``httpx.AsyncClient`` 绑定首次使用它的事件循环
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None
        self._init_lock: Optional[asyncio.Lock] = None
        self._init_lock_loop: Optional[asyncio.AbstractEventLoop] = None

    # -- 生命周期 ---------------------------------------------------------

    @property
    def auth_enabled(self) -> bool:
        """是否配置了鉴权 token。"""
        return bool(self._auth_token)

    @property
    def is_closed(self) -> bool:
        """连接池是否已关闭（尚未创建时也视为已关闭）。"""
        return self._client is None or self._client.is_closed

    @property
    def docs_url(self) -> str:
        """服务端交互式文档地址（``/docs``，协议 §2.1 说明其始终公开）。"""
        return f"{self.base_url}/docs"

    def _default_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if self._auth_token:
            if self._auth_scheme in ("x-api-token", "both"):
                headers["X-API-Token"] = self._auth_token
            if self._auth_scheme in ("bearer", "both"):
                headers["Authorization"] = f"Bearer {self._auth_token}"
        headers.update({str(key): str(value) for key, value in self._extra_headers.items()})
        return headers

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout,
            headers=self._default_headers(),
            transport=self._transport,
            follow_redirects=True,
        )

    async def _ensure_client(self) -> httpx.AsyncClient:
        """返回当前事件循环可用的连接池，必要时（首次/关闭/换循环）重建。"""
        loop = asyncio.get_running_loop()
        client = self._client
        if client is not None and not client.is_closed and self._client_loop is loop:
            return client
        if self._init_lock is None or self._init_lock_loop is not loop:
            self._init_lock = asyncio.Lock()
            self._init_lock_loop = loop
        async with self._init_lock:
            client = self._client
            if client is not None and (client.is_closed or self._client_loop is not loop):
                # 同一循环内的旧连接池可以优雅关闭；跨循环的不能 await，直接丢弃
                if self._client_loop is loop:
                    await client.aclose()
                else:
                    logger.debug("检测到事件循环变化，丢弃旧的 HTTP 连接池：%s", self.base_url)
                client = None
                self._client = None
                self._client_loop = None
            if client is None:
                client = self._build_client()
                self._client = client
                self._client_loop = loop
                logger.debug("已创建远程知识图谱连接池：%s", self.base_url)
            return client

    async def start(self) -> "RemoteKGTool":
        """提前创建连接池（可选；并发首调时线程安全）。"""
        await self._ensure_client()
        return self

    async def aclose(self) -> None:
        """关闭连接池；重复调用安全。"""
        client, self._client = self._client, None
        self._client_loop = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def close(self) -> None:
        """:meth:`aclose` 的别名，便于 ``await client.close()`` 的写法。"""
        await self.aclose()

    async def __aenter__(self) -> "RemoteKGTool":
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(base_url={self.base_url!r}, "
            f"auth={'on' if self.auth_enabled else 'off'}, "
            f"timeout={self._timeout!r}, max_retries={self._max_retries})"
        )

    @classmethod
    def from_settings(
        cls,
        settings: Optional[Mapping[str, Any]],
        *,
        env: Optional[Mapping[str, str]] = None,
        section: str = "remote_kg",
    ) -> Optional["RemoteKGTool"]:
        """按「环境变量 > 配置文件 > 代码默认值」构造客户端；未启用时返回 ``None``。

        优先级必须在这里显式实现：构造函数只在参数为 ``None`` 时才读环境变量
        （见 :meth:`__init__`），若把配置值直接当参数传进去，环境变量就会被静默跳过。

        :param settings: 已解析的配置字典（如 ``setting.json`` 的内容）
        :param env: 环境变量映射，默认读 ``os.environ``（测试时可注入）
        :param section: 配置段名，默认 ``remote_kg``
        :return: 配置了 ``enabled: true`` 时返回客户端，否则返回 ``None``

        配置段示例::

            "remote_kg": {
                "enabled": true,
                "base_url": "http://127.0.0.1:8000",
                "auth_token": "",
                "auth_scheme": "x-api-token",
                "timeout": 20,
                "max_retries": 1
            }
        """
        cfg = dict((settings or {}).get(section) or {})
        if not cfg.get("enabled"):
            return None

        source: Mapping[str, str] = os.environ if env is None else env
        # 空串/纯空白一律视为"没设"，否则会把配置文件里的值与默认值一起顶掉
        env_base = str(source.get("REMOTE_KG_BASE_URL") or "").strip()
        cfg_base = str(cfg.get("base_url") or "").strip()
        base_url = env_base or cfg_base or DEFAULT_BASE_URL

        env_token = str(source.get("REMOTE_KG_API_TOKEN") or "").strip()
        cfg_token = str(cfg.get("auth_token") or "").strip()
        auth_token = env_token or cfg_token
        return cls(
            base_url,
            auth_token=auth_token,
            auth_scheme=str(cfg.get("auth_scheme") or "x-api-token"),
            timeout=cfg.get("timeout", DEFAULT_TIMEOUT),
            max_retries=cfg.get("max_retries", 2),
        )

    # -- 底层请求 ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        body: Any = None,
    ) -> Any:
        """发送请求并处理重试与错误映射。

        :param method: HTTP 方法（``GET``/``POST``）
        :param path: 以 ``/`` 开头的接口路径
        :param params: query 参数（字典或 ``(key, value)`` 序列，列表值会展开成重复参数）
        :param body: JSON 请求体；``None`` 表示不带请求体
        :return: 解析后的 JSON（空响应体返回 ``{}``）
        """
        client = await self._ensure_client()
        encoded_params = _encode_params(params)
        attempts = self._max_retries + 1
        last_error: Optional[RemoteKGError] = None

        for attempt in range(1, attempts + 1):
            try:
                response = await client.request(
                    method, path, params=encoded_params, json=body
                )
            except httpx.TimeoutException as exc:
                last_error = RemoteKGTimeoutError(
                    f"{method} {path} 请求超时：{exc}",
                    url=f"{self.base_url}{path}",
                )
            except httpx.HTTPError as exc:
                last_error = RemoteKGConnectionError(
                    f"{method} {path} 连接失败：{exc}",
                    url=f"{self.base_url}{path}",
                )
            else:
                if response.status_code < 400:
                    return _decode_json(response)
                last_error = _error_from_response(response, method, path)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise last_error

            if attempt < attempts:
                delay = self._retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "%s 第 %d/%d 次尝试失败，%.2f 秒后重试：%s",
                    f"{method} {path}",
                    attempt,
                    attempts,
                    delay,
                    last_error,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

        if last_error is None:  # 理论不可达：循环体内每条分支都会赋值
            raise RemoteKGError(f"{method} {path} 请求失败，但未记录到具体错误")
        raise last_error

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        body: Any = None,
    ) -> Any:
        """原始请求入口（协议新增端点或调试时使用）。

        正常情况下请用下面的具名方法，它们自带参数校验。

        :raises RemoteKGError: 见 :class:`RemoteKGError` 的层次结构
        """
        normalized = path if path.startswith("/") else f"/{path}"
        return await self._request(method.upper(), normalized, params=params, body=body)

    # -- 1. 工作状态 -------------------------------------------------------

    async def get_status(
        self, probe_llm: bool = True, refresh: bool = False
    ) -> dict[str, Any]:
        """GET /retrieval/status：调用前自检，三项全绿（``ok=true``）再继续检索。

        :param probe_llm: 是否真的调一次大模型做连通性探测；``False`` 时只报告
            "配置是否齐全"（高频轮询建议 ``False``）
        :param refresh: 忽略服务端 30 秒探测缓存，强制重新探测
        :return: ``ok`` / ``version`` / ``checked_at`` / ``components.{graph,neo4j,llm}``
        """
        params = {"probe_llm": probe_llm, "refresh": refresh}
        return await self._request("GET", "/retrieval/status", params=params)

    # -- 2. Schema 元信息 ---------------------------------------------------

    async def get_schema(self) -> dict[str, Any]:
        """GET /retrieval/schema：确认"图里有什么"，再决定用哪种检索方式。

        :return: ``labels`` / ``relationship_types`` / ``property_keys`` / ``indexes`` /
            ``constraints`` / ``entity_types`` / ``relation_hint`` / ``graph_model`` / ``search``
        """
        return await self._request("GET", "/retrieval/schema")

    # -- 3. 关键词检索 ------------------------------------------------------

    async def keyword_search(
        self,
        query: str,
        *,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 10,
        document_ids: Optional[Sequence[str]] = None,
        use_post: bool = False,
    ) -> dict[str, Any]:
        """关键词检索：已知关键词时找实体/分块/文档，比语义检索便宜。

        对应 ``GET /retrieval/keywords``（默认）与 ``POST /retrieval/keywords``（``use_post=True``），
        两者查询语义等价，区别只在提交方式与服务端字段名（见下方 note）。

        :param query: 查询串，按空格与中英文标点切词后分别匹配
        :param kinds: 只查哪些通道，取值 ``entity``/``chunk``/``document``；``None`` 表示三路都查
        :param limit: **每个通道**的返回上限，1~50（协议 §2.4）
        :param document_ids: 只在这些文档的分块里检索
        :param use_post: ``True`` 时用 POST 提交（长查询串不受 URL 长度限制）
        :return: ``terms`` / ``entities`` / ``chunks`` / ``documents`` / ``elapsed_ms``
        :raises ValueError: ``query`` 为空或参数越界

        .. note::
           GET 与 POST 的**参数名不同**（与协议 §5 的两个 ``curl`` 示例一致）：
           GET 走 query 参数 ``q``，POST 走请求体字段 ``query``；只传 ``query`` 给
           GET 会被服务端判 422。本方法已在内部处理，调用方只用 ``query`` 一个入口。
        """
        text = _require_text("query", query)
        normalized_kinds = _text_list("kinds", kinds, allowed=_KINDS)
        normalized_doc_ids = _text_list("document_ids", document_ids)
        normalized_limit = _bounded_int("limit", limit, _LIMIT_KEYWORD)

        if use_post:
            body: dict[str, Any] = {"query": text, "limit": normalized_limit}
            if normalized_kinds:
                body["kinds"] = normalized_kinds
            if normalized_doc_ids:
                body["document_ids"] = normalized_doc_ids
            return await self._request("POST", "/retrieval/keywords", body=body)

        params: list[tuple[str, Any]] = [("q", text), ("limit", normalized_limit)]
        params += [("kinds", kind) for kind in normalized_kinds]
        params += [("document_ids", doc_id) for doc_id in normalized_doc_ids]
        return await self._request("GET", "/retrieval/keywords", params=params)

    # -- 4. 语义检索 --------------------------------------------------------

    async def semantic_search(
        self,
        query: str,
        *,
        entity_limit: int = 10,
        relation_limit: int = 10,
        min_score: float = 0.0,
        entity_types: Optional[Sequence[str]] = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """语义检索：不要求字面重合，按语义找实体与关系（协议 §6）。

        :param query: 自然语言查询，例如"谁创建了这家公司"
        :param entity_limit: 实体结果上限，1~50
        :param relation_limit: 关系结果上限，1~50
        :param min_score: 低于该分数的结果不返回，0~1
        :param entity_types: 只在这些实体类型里检索；``None`` 表示不限
        :param refresh: 忽略服务端 20 秒实体目录缓存，重新读库
        :return: ``mode``（``embedding``/``lexical``）/ ``embedding_enabled`` /
            ``entities`` / ``relations`` / ``elapsed_ms``

        .. note::
           向量不可用时服务端自动降级为 ``mode=lexical``，接口不会报错；
           ``score`` 只在同一次响应内可比。
        """
        text = _require_text("query", query)
        normalized_types = _text_list("entity_types", entity_types)
        body: dict[str, Any] = {
            "query": text,
            "entity_limit": _bounded_int("entity_limit", entity_limit, _LIMIT_SEMANTIC),
            "relation_limit": _bounded_int("relation_limit", relation_limit, _LIMIT_SEMANTIC),
            "min_score": _bounded_float("min_score", min_score, (0.0, 1.0)),
            "refresh": bool(refresh),
        }
        if normalized_types:
            body["entity_types"] = normalized_types
        return await self._request("POST", "/retrieval/semantic", body=body)

    # -- 5. 图遍历 ----------------------------------------------------------

    async def traverse(
        self,
        start: str,
        *,
        start_kind: str = "auto",
        depth: int = 2,
        relation_types: Optional[Sequence[str]] = None,
        direction: str = "both",
        node_limit: int = 100,
        edge_limit: int = 300,
    ) -> dict[str, Any]:
        """从起点做有界 BFS，返回子图（节点 + 边）。

        :param start: 起点节点 ID 或实体名/别名（``start_kind=auto`` 时先按 ID 再按名称解析）
        :param start_kind: ``auto``/``entity``/``document``/``chunk``
        :param depth: 跳数，1~4；深度越大结果爆炸越快，建议先 1~2（协议 §13）
        :param relation_types: 只走这些关系类型，如 ``["RELATES", "MENTIONS"]``
        :param direction: ``out``（出边）/ ``in``（入边）/ ``both``
        :param node_limit: 子图节点数上限，1~500
        :param edge_limit: 子图边数上限，1~2000
        :return: ``start``（解析后的起点 ID）/ ``nodes`` / ``edges`` / ``truncated`` / ``elapsed_ms``
        :raises RemoteKGNotFoundError: 起点找不到（404）
        """
        body: dict[str, Any] = {
            "start": _require_text("start", start),
            "start_kind": _choice("start_kind", start_kind, _START_KINDS),
            "depth": _bounded_int("depth", depth, _LIMIT_DEPTH),
            "direction": _choice("direction", direction, _DIRECTIONS),
            "node_limit": _bounded_int("node_limit", node_limit, _LIMIT_NODE),
            "edge_limit": _bounded_int("edge_limit", edge_limit, _LIMIT_EDGE),
        }
        types = _text_list("relation_types", relation_types)
        if types:
            body["relation_types"] = types
        return await self._request("POST", "/retrieval/traverse", body=body)

    # -- 6. 路径查找 --------------------------------------------------------

    async def find_paths(
        self,
        source: str,
        target: str,
        *,
        max_depth: int = 4,
        relation_types: Optional[Sequence[str]] = None,
        direction: str = "both",
        limit: int = 3,
    ) -> dict[str, Any]:
        """查两个节点之间的**最短**路径，回答"这两个人/公司是怎么连上的"。

        :param source: 起点：节点 ID 或实体名
        :param target: 终点：节点 ID 或实体名
        :param max_depth: 最大跳数，1~8
        :param relation_types: 只允许这些关系类型
        :param direction: ``out``/``in``/``both``
        :param limit: 最多返回几条最短路径，1~10
        :return: ``found`` / ``source`` / ``target`` / ``paths`` / ``elapsed_ms``

        .. note::
           服务端用 ``allShortestPaths``，只返回最短的若干条；``source == target`` 视为未找到；
           端点不存在抛 :class:`RemoteKGNotFoundError`。
        """
        body: dict[str, Any] = {
            "source": _require_text("source", source),
            "target": _require_text("target", target),
            "max_depth": _bounded_int("max_depth", max_depth, _LIMIT_PATH_DEPTH),
            "direction": _choice("direction", direction, _DIRECTIONS),
            "limit": _bounded_int("limit", limit, _LIMIT_PATH_COUNT),
        }
        types = _text_list("relation_types", relation_types)
        if types:
            body["relation_types"] = types
        return await self._request("POST", "/retrieval/paths", body=body)

    # -- 7. 读取节点正文 ----------------------------------------------------

    async def get_node(
        self,
        node_id: str,
        *,
        max_chars: int = 4000,
        include_related: bool = True,
    ) -> dict[str, Any]:
        """读单个节点的正文（协议 §9.1）。

        ``body.text`` 的含义随节点类型不同：``chunk`` 是分块原文，``document`` 是全部
        分块按 ``index`` 拼接的全文，``entity`` 是实体描述，``other`` 为空串。

        :param node_id: 节点 ID（``Document``/``Chunk``/``Entity`` 跨类型唯一）
        :param max_chars: 正文字符上限，200~40000；超出截断并置 ``truncated=true``
        :param include_related: 是否返回关联片段（实体→提及它的分块；文档→前 3 个分块）
        :return: ``node`` / ``body`` / ``related``
        :raises RemoteKGNotFoundError: 节点不存在（404）
        """
        quoted = _quote_node_id("node_id", node_id)
        params = {
            "max_chars": _bounded_int("max_chars", max_chars, _LIMIT_MAX_CHARS),
            "include_related": include_related,
        }
        return await self._request("GET", f"/retrieval/nodes/{quoted}", params=params)

    async def get_nodes_bodies(
        self,
        ids: Sequence[str],
        *,
        max_chars: int = 4000,
    ) -> dict[str, Any]:
        """批量读节点正文（协议 §9.2）：单次最多 50 个节点，逐条失败不影响其它条。

        :param ids: 1~50 个节点 ID
        :param max_chars: 每个节点的正文上限，200~40000
        :return: ``nodes``（同单个接口的完整结构）与 ``missing``（不存在的 ID）
        :raises ValueError: ``ids`` 为空或超过 50 个
        """
        normalized_ids = _text_list("ids", ids)
        if not normalized_ids:
            raise ValueError("参数 ids 至少要包含 1 个节点 ID")
        low, high = _LIMIT_BATCH_IDS
        if not low <= len(normalized_ids) <= high:
            raise ValueError(f"参数 ids 一次最多 {high} 个节点，收到 {len(normalized_ids)} 个")
        body = {
            "ids": normalized_ids,
            "max_chars": _bounded_int("max_chars", max_chars, _LIMIT_MAX_CHARS),
        }
        return await self._request("POST", "/retrieval/nodes/bodies", body=body)

    # -- 8. 实体识别与消歧 --------------------------------------------------

    async def resolve_entities(
        self,
        text: str,
        *,
        use_llm: bool = True,
        top_k: int = 3,
        min_score: float = 0.55,
        entity_types: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """把文本里的实体提及识别出来，并对齐到图谱里的实体节点。

        :param text: 待分析文本，不超过 20000 字符（过长请先切段，协议 §13）
        :param use_llm: 是否先用大模型抽取；失败/未配置时自动退回词典匹配
        :param top_k: 每个提及最多几个候选，1~10
        :param min_score: 判定"已消歧"的最低分，0~1
        :param entity_types: 只在这些实体类型里匹配
        :return: ``extractor``（``llm``/``dictionary``/``hybrid``）/ ``mentions`` / ``elapsed_ms``

        .. note::
           ``use_llm=true`` 会消耗一次模型调用；上游模型失败时服务端返回 502，
           但不会中断词典兜底支路。
        """
        normalized_text = _require_text("text", text)
        if len(normalized_text) > _RESOLVE_TEXT_MAX:
            raise ValueError(
                f"参数 text 超过 {_RESOLVE_TEXT_MAX} 字符上限（实际 {len(normalized_text)}），请先切段"
            )
        normalized_types = _text_list("entity_types", entity_types)
        body: dict[str, Any] = {
            "text": normalized_text,
            "use_llm": bool(use_llm),
            "top_k": _bounded_int("top_k", top_k, _LIMIT_TOP_K),
            "min_score": _bounded_float("min_score", min_score, (0.0, 1.0)),
        }
        if normalized_types:
            body["entity_types"] = normalized_types
        return await self._request("POST", "/retrieval/entities/resolve", body=body)

    # -- 附：公开的健康检查 ------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health：进程存活检查（始终公开，不参与鉴权）。"""
        return await self._request("GET", "/health")

    async def ready(self) -> dict[str, Any]:
        """GET /ready：就绪检查（始终公开，不参与鉴权）。"""
        return await self._request("GET", "/ready")


# ---------------------------------------------------------------------------
# 手工自测：``python -m document_agent.retrieve_tool.remote_kg_tool``
# ---------------------------------------------------------------------------


async def _demo() -> int:
    """按协议 §11 的最小闭环跑一遍，服务未启动时给出可读提示。"""
    base_url = os.getenv("REMOTE_KG_BASE_URL", DEFAULT_BASE_URL)
    async with RemoteKGTool(base_url) as kg:
        print(f"服务地址：{kg.base_url}    鉴权：{'开' if kg.auth_enabled else '关'}")
        try:
            status = await kg.get_status(probe_llm=False)
        except RemoteKGError as exc:
            print(f"无法访问检索服务：{exc}")
            return 1

        print(f"状态：ok={status.get('ok')} version={status.get('version')}")
        for name, component in (status.get("components") or {}).items():
            print(f"  - {name}: ok={component.get('ok')} error={component.get('error') or '-'}")
        if not status.get("ok"):
            print("三项未全绿，建议先排查 components.*.error（协议 §3）")
            return 1

        schema = await kg.get_schema()
        print(f"标签：{schema.get('labels')}")
        print(f"关系类型：{schema.get('relationship_types')}")

        hits = await kg.semantic_search("知识图谱", entity_limit=3, relation_limit=3)
        print(f"语义检索：mode={hits.get('mode')} 实体 {len(hits.get('entities') or [])} 个")
        entities = hits.get("entities") or []
        if entities:
            first = entities[0]
            print(f"命中实体：{first['name']}（{first['id']}）")
            subgraph = await kg.traverse(first["id"], depth=1, node_limit=20, edge_limit=40)
            print(
                f"1 跳子图：{len(subgraph.get('nodes') or [])} 节点 / "
                f"{len(subgraph.get('edges') or [])} 边，truncated={subgraph.get('truncated')}"
            )
            chunks = [node["id"] for node in subgraph.get("nodes") or [] if node["kind"] == "chunk"]
            if chunks:
                bodies = await kg.get_nodes_bodies(chunks[:3], max_chars=400)
                for node in bodies.get("nodes") or []:
                    print(f"  片段 {node['node']['id']}：{node['body']['text'][:80]}...")
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(_demo()))
