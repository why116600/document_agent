from __future__ import annotations

import os
import json
import httpx
import re
from functools import lru_cache
from typing import TypeVar, Type, Optional, Union, List
from pydantic import BaseModel

from langchain_openai import ChatOpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

def get_api_key(interactive: bool = True) -> str:
    """
    从环境变量读取 MODEL_API_KEY，未设置时尝试在控制台交互式提示输入。
    """
    api_key = os.getenv("MODEL_API_KEY")
    if not api_key and interactive:
        try:
            api_key = input("未检测到环境变量 MODEL_API_KEY，请输入 API Key（仅在本次运行中有效）：").strip()
            if api_key:
                os.environ["MODEL_API_KEY"] = api_key
        except (EOFError, KeyboardInterrupt):
            pass

    if not api_key:
        raise ValueError(
            "未检测到 MODEL_API_KEY 环境变量。\n"
            "请通过以下命令设置环境变量后重试：\n"
            "  PowerShell: $env:MODEL_API_KEY=\"你的API密钥\"\n"
            "  CMD:        set MODEL_API_KEY=你的API密钥\n"
            "  Bash:       export MODEL_API_KEY=\"你的API密钥\""
        )
    return api_key

@lru_cache(maxsize=1)
def get_deepseek_llm() -> ChatOpenAI:
    """
    Create a LangChain Chat model for DeepSeek (OpenAI-compatible API).

    Reads config from environment variables:
      - MODEL_BASE_URL    (default: https://api.deepseek.com/v1)
      - MODEL_NAME        (default: deepseek-chat)
      - MODEL_API_KEY     (required)
    """
    base_url = os.getenv("MODEL_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("MODEL_NAME", DEFAULT_MODEL)
    api_key = get_api_key()

    # temperature=0 makes planning more consistent.
    # 生成内容越多，耗时越长，timeout设置短了会超时
    # 超时之所以没返回，是因为内部有重试机制，重试次数由max_retries参数决定
    return ChatOpenAI(
        model=model,#"starlight/DeepSeek-V4",#
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        timeout=120,
        max_retries=1,
        extra_body={
        "thinking": {"type": "disabled"}  # ✅ 关键：关闭思考模式
        }
    )
    
def llm_invoke(llm,prompt : str,retry=3):
    for _ in range(retry):
        try:
            res=llm.invoke(prompt)
            return res
        except httpx.TimeoutException as e:
            print(f"大模型请求超时: {e}")
        except Exception as e:
            print(f"大模型其他错误：{e}")
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

_extract_json_from_text = extract_json_from_text


def _try_salvage_parsed_model(res, model_class: Type[T]) -> Optional[T]:
    """当 LangChain 的 function_calling 解析失败时，尝试从 raw 消息中拯救解析。"""
    raw = res.get("raw") if isinstance(res, dict) else None
    if raw is None:
        return None

    def _validate_data(data) -> Optional[T]:
        if isinstance(data, list):
            # 模型如果直接返回了数组列表，自动封装到 edits 或 items 字段
            if hasattr(model_class, "model_fields"):
                fields = model_class.model_fields
                if "edits" in fields:
                    data = {"summary": "批量修改方案", "edits": data}
                elif "items" in fields:
                    data = {"items": data}
        try:
            return model_class.model_validate(data)
        except Exception:
            return None

    # 1. 尝试从 raw.tool_calls 提取参数
    tool_calls = getattr(raw, "tool_calls", None) or []
    for call in tool_calls:
        args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
        if isinstance(args, (dict, list)) and args:
            salvaged = _validate_data(args)
            if salvaged is not None:
                return salvaged
        elif isinstance(args, str) and args.strip():
            try:
                data = json.loads(args)
                salvaged = _validate_data(data)
                if salvaged is not None:
                    return salvaged
            except Exception:
                pass

    # 2. 尝试从 raw.content 文本中提取 JSON
    content = getattr(raw, "content", "")
    if isinstance(content, str) and content.strip():
        data = _extract_json_from_text(content)
        if data is not None:
            salvaged = _validate_data(data)
            if salvaged is not None:
                return salvaged
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
    model_llm = llm.with_structured_output(model_class, method="function_calling", include_raw=True)
    for _ in range(retry):
        try:
            res = model_llm.invoke(prompt)
            if res and "parsed" in res and res["parsed"] is not None:
                return res["parsed"]
            else:
                salvaged = _try_salvage_parsed_model(res, model_class)
                if salvaged is not None:
                    print(f"从原始响应成功修复并解析出 {model_class.__name__}")
                    return salvaged
                err_msg = res.get("parsing_error") if isinstance(res, dict) else None
                print(f"LLM response could not be parsed into {model_class.__name__}." + (f" 原因: {err_msg}" if err_msg else ""))
        except httpx.TimeoutException as e:
            print(f"LLM request timed out: {e}")
        except Exception as e:
            print(f"Error invoking LLM or parsing response: {e}")
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