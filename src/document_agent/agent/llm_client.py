from __future__ import annotations

import os
import sys
import json
import httpx
import re
from functools import lru_cache
from urllib.parse import urlsplit
from typing import TypeVar, Type, Optional, Union, List
from pydantic import BaseModel

from langchain_openai import ChatOpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


class ModelRequestError(RuntimeError):
    """服务端拒绝了模型请求；重复同一请求无法修复配置或参数错误。"""


def describe_model_error(exc: Exception) -> str:
    """保留响应正文帮助定位400等错误，不输出密钥或请求头。"""
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    body = getattr(exc, "body", None)
    if body is None and response is not None:
        try:
            body = response.json()
        except Exception:
            body = getattr(response, "text", None)
    detail = json.dumps(body, ensure_ascii=False, default=str) if body is not None else str(exc)
    key = os.getenv("MODEL_API_KEY", "")
    if key:
        detail = detail.replace(key, "<API_KEY已隐藏>")
    detail = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer <已隐藏>", detail)
    request_id = getattr(exc, "request_id", None)
    metadata = []
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("content-type", "server", "via", "x-request-id", "x-ds-trace-id", "cf-ray"):
            value = headers.get(name)
            if isinstance(value, str) and value:
                metadata.append(f"{name}={value[:200]}")
    if metadata:
        detail += "；响应标识：" + "，".join(metadata)
        if key:
            detail = detail.replace(key, "<API_KEY已隐藏>")
    prefix = f"HTTP {status}" if status else type(exc).__name__
    return f"{prefix}：{detail[:2500]}" + (f"；request_id={request_id}" if request_id else "")


def _raise_if_request_rejected(exc: Exception) -> None:
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if status in {400, 401, 403, 404, 405, 413, 415, 422}:
        raise ModelRequestError(
            "模型接口拒绝请求，已停止重复调用。" + describe_model_error(exc)
            + "。请核对同一服务商的 MODEL_BASE_URL、MODEL_NAME、MODEL_API_KEY 及请求参数。"
        ) from None

def validate_api_key(api_key):
    """在发送HTTP前拒绝空白和控制字符；错误消息不包含密钥。"""
    if not api_key:
        raise ValueError("MODEL_API_KEY environment variable is not set.")
    if any(not 33 <= ord(char) <= 126 for char in api_key):
        raise ValueError(
            "MODEL_API_KEY 含控制字符、空白或非ASCII字符，未发送请求。"
            "请用明文 Read-Host 重新输入完整API Key；隐藏输入时粘贴可能没有成功。"
        )
    return api_key


def get_api_key():
    """从当前进程的环境变量读取并校验密钥。"""
    return validate_api_key(os.getenv("MODEL_API_KEY"))


@lru_cache(maxsize=1)
def get_deepseek_llm() -> ChatOpenAI:
    """
    Create a LangChain Chat model for DeepSeek (OpenAI-compatible API).

    Reads config from environment variables:
      - MODEL_BASE_URL    (default: https://api.deepseek.com)
      - MODEL_NAME        (default: deepseek-flash)
      - MODEL_API_KEY     (required)
    """
    base_url = (os.getenv("MODEL_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.getenv("MODEL_NAME") or DEFAULT_MODEL).strip()
    official_deepseek = urlsplit(base_url).hostname == "api.deepseek.com"
    if official_deepseek and model in {"deepseek-chat", "deepseek-reasoner"}:
        raise ValueError(
            f"DeepSeek 官方已停用旧模型名 {model}；请将 MODEL_NAME 改为 deepseek-flash，"
            "或你确认可用的其他官方模型名。"
        )

    api_key = get_api_key()

    # 官方新模型显式采用非思考模式，避免给现有结构化调用链引入
    # reasoning_content 回传要求；第三方兼容服务不附加此专用参数。
    # 生成内容越多，耗时越长，timeout设置短了会超时
    # 超时之所以没返回，是因为内部有重试机制，重试次数由max_retries参数决定
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        timeout=120,
        max_retries=1,
        extra_body={"thinking": {"type": "disabled"}} if official_deepseek else None,
    )
    
def llm_invoke(llm,prompt : str,retry=3):
    for _ in range(retry):
        try:
            res=llm.invoke(prompt)
            return res
        except httpx.TimeoutException as e:
            print(f"大模型请求超时: {e}")
        except Exception as e:
            _raise_if_request_rejected(e)
            print(f"大模型其他错误：{describe_model_error(e)}")
    return {"raw":"","parsed":None}

# 定义类型变量，限定为 BaseModel 的子类
T = TypeVar('T', bound=BaseModel)

def extract_json_from_text(text: str, return_all: bool = False) -> Optional[Union[dict, list, List[str]]]:
    """从文本中提取 JSON 对象或数组。

    :param text: 待提取的文本内容
    :param return_all: 若为 True，返回提取出的所有合法 JSON 字符串列表；若为 False，返回首个成功解析出的 Python 对象（dict 或 list）。
    """
    if not text or not isinstance(text, str):
        return [] if return_all else None

    results = []
    decoder = json.JSONDecoder()
    pattern = re.compile(r'[\{\[]')

    for match in pattern.finditer(text):
        start_index = match.start()
        try:
            obj, end_index = decoder.raw_decode(text, start_index)
            if return_all:
                results.append(text[start_index:end_index])
            else:
                return obj
        except json.JSONDecodeError:
            continue

    if return_all:
        return results
    return None


def llm_model_invoke(llm, prompt: str, model_class: Type[T], retry=3) -> T | None:
    """
    Invoke the LLM and parse the response into a Pydantic model.

    :param llm: The LLM instance to invoke.
    :param prompt: The prompt to send to the LLM.
    :param model_class: The Pydantic model class to parse the response into.
    :param retry: Number of retries in case of failure.
    :return: An instance of model_class or None if parsing fails.
    """
    model_llm=llm.with_structured_output(model_class,method="function_calling", include_raw=True)
    for _ in range(retry):
        try:
            res = model_llm.invoke(prompt)
            if res and 'parsed' in res and res['parsed'] is not None:
                return res['parsed']
            else:
                print(f"LLM response could not be parsed into {model_class.__name__}.")
        except httpx.TimeoutException as e:
            print(f"LLM request timed out: {e}")
        except Exception as e:
            _raise_if_request_rejected(e)
            print(f"Error invoking LLM or parsing response: {describe_model_error(e)}")
    return None

if __name__ == "__main__":
    # from openai import OpenAI
    # client=OpenAI(base_url=DEFAULT_BASE_URL, api_key=get_api_key())
    # completion = client.chat.completions.create(model="starlight/DeepSeek-V4", messages=[
    # {"role": "user", "content": "你是谁？"}]
    # )
    # print(completion.choices[0].message)
    client=get_deepseek_llm()
    res=client.invoke("你是谁？")
    print(res)
