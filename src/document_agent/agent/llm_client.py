from __future__ import annotations

import os
import sys
import json
import httpx
import re
from functools import lru_cache
from typing import TypeVar, Type, Optional, Union, List
from pydantic import BaseModel

from langchain_openai import ChatOpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

def get_api_key():
    """
    Get the DeepSeek API key from environment variable or prompt the user for it.
    """
    api_key = os.getenv("MODEL_API_KEY")
    if not api_key:
        raise ValueError("MODEL_API_KEY environment variable is not set.")
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