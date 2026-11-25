from __future__ import annotations

import os
import sys
import json
import httpx
import re
from functools import lru_cache
from typing import List, Optional, Union

from langchain_openai import ChatOpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
# DeepSeek V4 系列默认 thinking，且 thinking 模式不接受 tool_choice；
# 产品结构化输出依赖 tool_choice，因此默认对这类模型关闭 thinking。
_THINKING_TOGGLE_MODELS = ("deepseek-flash", "deepseek-v4-pro")


def _thinking_disabled(model: str) -> bool:
    """按环境变量和模型名决定是否关闭 thinking。

    MODEL_THINKING=on/off 可强制覆盖；auto（默认）只对已知冲突模型关闭，
    避免给不支持该参数的 OpenAI 兼容模型发送额外字段。
    """
    policy = (os.getenv("MODEL_THINKING") or "auto").strip().lower()
    if policy in ("on", "enabled", "1", "true"):
        return False
    if policy in ("off", "disabled", "0", "false"):
        return True
    return any(name in model.lower() for name in _THINKING_TOGGLE_MODELS)

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
    thinking_off = _thinking_disabled(model)
    kwargs = {}
    if thinking_off:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    print(f"【模型】{model} thinking={'off' if thinking_off else 'on'}")
    return ChatOpenAI(
        model=model,#"starlight/DeepSeek-V4",#
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        timeout=120,
        max_retries=1,
        **kwargs,
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