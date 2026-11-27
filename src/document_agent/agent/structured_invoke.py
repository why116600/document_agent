"""结构化输出调用：解析诊断、纠错重试与可恢复解析。
- 本模块负责 ``with_structured_output`` 的重试策略、解析错误诊断，
  以及仅移除空值可选字段的可恢复解析。

业务字段的语义校验（例如 ``management_action`` 的空值写法）仍放在各节点
自己的 Pydantic 模型中，避免把文档业务规则下沉到通用调用层。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "unknown"  # 仅用于诊断兜底；优先读取 llm.model_name 或环境变量
NULL_LIKE_STRINGS = frozenset({"null", "none", "nil", "undefined", "n/a"})


def _format_parsing_error(error: Any, limit: int = 800) -> str:
    """把结构化解析错误压缩成可读的一行，避免把完整模型输出或用户原文带进日志。"""
    if error is None:
        return ""
    if isinstance(error, ValidationError):
        parts = []
        for item in error.errors()[:3]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "(根)"
            parts.append(f"{location}: {item.get('msg')}")
        text = "；".join(parts)
    else:
        text = f"{type(error).__name__}: {error}"
    return text.strip()[:limit]


def _tool_args(raw: Any) -> Optional[Dict[str, Any]]:
    """从 include_raw 的原始响应里取出工具调用参数，供可恢复解析使用。"""
    calls = getattr(raw, "tool_calls", None) or []
    if not calls:
        return None
    call = calls[0]
    args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    return args if isinstance(args, dict) else None


def _salvage_model(model_class: Type[T], data: Optional[Dict[str, Any]]) -> Tuple[T | None, List[str]]:
    """只移除“值为空字符串”的可选字段后重试校验，保留 task_type 等核心字段。

    仅处理单字段、非必填、且值属于 null/None/空串的错误；其它校验失败不在此处吞掉。
    """
    if not isinstance(data, dict):
        return None, []
    candidate = dict(data)
    removed: List[str] = []
    for _ in range(3):
        try:
            return model_class.model_validate(candidate), removed
        except ValidationError as exc:
            fields = []
            for item in exc.errors():
                location = item.get("loc") or ()
                if len(location) != 1:
                    continue
                name = location[0]
                field = model_class.model_fields.get(name)
                value = candidate.get(name)
                if (field is not None and not field.is_required()
                        and isinstance(value, str)
                        and value.strip().lower() in NULL_LIKE_STRINGS):
                    fields.append(name)
            fields = list(dict.fromkeys(fields))
            if not fields:
                return None, []
            for name in fields:
                candidate.pop(name, None)
                removed.append(name)
    return None, []


def _message_text(response: Any) -> str:
    """把模型响应压成文本，兼容字符串、AIMessage 和多段内容。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _extract_json_payload(text: str) -> str:
    """从普通文本里取出 JSON 片段；只做格式恢复，不做业务校验。"""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def llm_model_invoke_detailed(llm, prompt: str, model_class: Type[T], retry=3,
                              feedback: bool = True, salvage: bool = True,
                              fallback_policy: str = "none"):
    """调用模型并返回 (解析结果, 诊断信息)。

    诊断信息只包含尝试次数、最后错误、被忽略的可选字段与模型标识，不记录用户原文。
    """
    model_llm = llm.with_structured_output(model_class, method="function_calling", include_raw=True)
    model_name = (getattr(llm, "model_name", None) or getattr(llm, "model", None)
                  or os.getenv("MODEL_NAME", DEFAULT_MODEL))
    diagnostics: Dict[str, Any] = {
        "attempts": 0,
        "last_error": "",
        "salvaged_fields": [],
        "model": str(model_name),
        "fallback": None,
    }
    current_prompt = prompt
    for attempt in range(1, retry + 1):
        diagnostics["attempts"] = attempt
        try:
            res = model_llm.invoke(current_prompt)
        except httpx.TimeoutException as e:
            diagnostics["last_error"] = f"timeout: {e}"
            print(f"LLM request timed out: {e}")
            continue
        except Exception as e:
            diagnostics["last_error"] = f"{type(e).__name__}: {e}"
            print(f"Error invoking LLM or parsing response: {e}")
            continue
        if res and res.get('parsed') is not None:
            return res['parsed'], diagnostics

        parsing_error = res.get('parsing_error') if isinstance(res, dict) else None
        diagnostics["last_error"] = _format_parsing_error(parsing_error)
        if salvage:
            salvaged, removed = _salvage_model(
                model_class, _tool_args(res.get('raw') if isinstance(res, dict) else None)
            )
            if salvaged is not None:
                diagnostics["salvaged_fields"] = removed
                print(f"【结构化输出】忽略空值可选字段 {removed}，已恢复 {model_class.__name__}")
                return salvaged, diagnostics
        if not diagnostics["last_error"]:
            diagnostics["last_error"] = "模型未返回可解析的结构化结果"
        print(f"LLM response could not be parsed into {model_class.__name__}: {diagnostics['last_error']}")
        if feedback and attempt < retry:
            current_prompt = prompt + (
                "\n\n【上次输出未通过结构校验】" + diagnostics["last_error"] +
                "\n请修正后重新输出。可空字段必须是 JSON null，不能是字符串 \"null\"、\"None\" 或空串。"
            )
    # 结构化输出降级：只解决“工具调用失败”，不改变任务语义。
    # 是否允许由调用方显式决定，意图解析保持默认 none。
    if fallback_policy == "json":
        diagnostics["fallback"] = "json"
        print(f"【结构化输出】{model_class.__name__} 工具调用失败，改用普通 JSON 重试")
        try:
            response = llm.invoke(prompt + "\n\n只输出合法 JSON，不要输出解释文字。")
        except Exception as exc:
            diagnostics["last_error"] = f"json_call:{type(exc).__name__}: {exc}"
            print(f"【结构化输出】JSON 降级调用失败：{exc}")
            return None, diagnostics
        try:
            payload = json.loads(_extract_json_payload(_message_text(response)))
            parsed = model_class.model_validate(payload)
            print(f"【结构化输出】JSON 降级恢复成功：{model_class.__name__}")
            return parsed, diagnostics
        except Exception as exc:
            diagnostics["last_error"] = f"json_schema:{type(exc).__name__}: {exc}"
            print(f"【结构化输出】JSON 降级后仍不合法：{exc}")
    return None, diagnostics


def llm_model_invoke(llm, prompt: str, model_class: Type[T], retry=3,
                     fallback_policy: str = "none") -> T | None:
    """
    Invoke the LLM and parse the response into a Pydantic model.

    :param llm: The LLM instance to invoke.
    :param prompt: The prompt to send to the LLM.
    :param model_class: The Pydantic model class to parse the response into.
    :param retry: Number of retries in case of failure.
    :return: An instance of model_class or None if parsing fails.
    """
    parsed, _ = llm_model_invoke_detailed(llm, prompt, model_class, retry=retry,
                                          feedback=False, salvage=False,
                                          fallback_policy=fallback_policy)
    return parsed


PLAIN_TEXT_INSTRUCTION = (
    "\n\n【降级输出】请直接输出本章正文：每段一行，不要输出章节标题、编号、"
    "Markdown 标记或任何解释文字。只输出正文段落本身。"
)


def _clean_plain_lines(text: str, max_paragraphs: int, max_chars: int) -> List[str]:
    """把纯文本响应整理成段落列表：去掉代码围栏、Markdown 标记与重复行。"""
    lines: List[str] = []
    total = 0
    for raw in (text or "").splitlines():
        line = str(raw).strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line).strip()
        line = re.sub(r"^[-*+]\s+", "", line).strip()
        if not line or line in {"{", "}", "[", "]", "json"}:
            continue
        if lines and lines[-1] == line:
            continue
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
        if len(lines) >= max_paragraphs:
            break
    return lines


def llm_text_paragraphs(llm, prompt: str, max_paragraphs: int = 24,
                        max_chars: int = 4000) -> List[str]:
    """结构化输出连续失败时的最后兜底：让模型用纯文本给出本章正文段落。

    只做输出格式降级，不改变任务语义；返回的段落仍会经过章节检查、
    长度上限与事实校验，不会绕过任何验收标准（DEF-010）。
    """
    from llm_client import llm_invoke  # 延迟导入，避免调用层与客户端互相引用

    try:
        response = llm_invoke(llm, prompt + PLAIN_TEXT_INSTRUCTION, retry=1)
    except Exception as exc:
        print(f"【结构化输出】纯文本降级调用失败：{exc}")
        return []
    # llm_invoke 失败时会返回 {'raw': '', 'parsed': None}，不能当成正文
    if isinstance(response, dict) and "parsed" in response:
        print("【结构化输出】纯文本降级未拿到有效响应")
        return []
    return _clean_plain_lines(_message_text(response), max_paragraphs, max_chars)
