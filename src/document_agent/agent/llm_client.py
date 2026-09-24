from __future__ import annotations

import os
import json
import httpx
import re
from functools import lru_cache
from typing import TypeVar, Type, Optional, Union, List
from pydantic import BaseModel

from langchain_openai import ChatOpenAI

from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

def _find_project_root() -> Path:
    """定位项目根目录（含 pyproject.toml 或 .git 的目录）"""
    curr = Path(__file__).resolve().parent
    for parent in [curr] + list(curr.parents):
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return Path.cwd()

def _load_dotenv_if_exists() -> None:
    """自动探测并加载 .env 配置文件中的环境变量"""
    search_dirs = [
        Path.cwd(),
        _find_project_root(),
        Path(__file__).resolve().parent,
    ]
    seen = set()
    for d in search_dirs:
        if d in seen:
            continue
        seen.add(d)
        env_file = d / ".env"
        if env_file.is_file():
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass

def get_api_key(interactive: bool = True) -> str:
    """
    从环境变量或 .env 读取 MODEL_API_KEY，未设置时尝试在控制台交互式提示输入，
    并可选择将输入的 key 保存到 .env 文件中免去重复输入。
    """
    _load_dotenv_if_exists()
    api_key = os.getenv("MODEL_API_KEY")
    if not api_key and interactive:
        try:
            api_key = input("未检测到环境变量 MODEL_API_KEY，请输入 API Key：").strip()
            if api_key:
                os.environ["MODEL_API_KEY"] = api_key
                try:
                    save_choice = input("是否将此 API Key 保存到项目根目录的 .env 文件中，下次自动加载免输？(Y/n) ").strip().lower()
                    if save_choice in ("", "y", "yes"):
                        root = _find_project_root()
                        env_file = root / ".env"
                        with open(env_file, "a", encoding="utf-8") as f:
                            f.write(f"\nMODEL_API_KEY={api_key}\n")
                        print(f"已将 API Key 保存至 {env_file}，后续运行无需再次手动输入。")
                except Exception:
                    pass
        except (EOFError, KeyboardInterrupt):
            pass

    if not api_key:
        raise ValueError(
            "未检测到 MODEL_API_KEY 环境变量。\n"
            "可通过以下方式配置（任选其一）：\n"
            "  1. 在项目根目录创建 .env 文件，写入：MODEL_API_KEY=你的API密钥\n"
            "  2. PowerShell 终端设置：$env:MODEL_API_KEY=\"你的API密钥\"\n"
            "  3. Windows 永久用户环境变量：[System.Environment]::SetEnvironmentVariable('MODEL_API_KEY', '你的密钥', 'User')"
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