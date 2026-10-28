"""改写范围契约：模型填 scope/action，代码只做可机械验证的校验。

本模块不依赖模型、不读写文档，便于离线自检。范围解析一律使用“大纲真实标题”，
不做语义猜测；解析不到就报错，绝不退回整篇。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

Container = Literal["body", "table", "header", "footer"]
Action = Literal["replace", "edit", "rewrite", "format"]

#动作 -> 允许的范围形态（format 暂不支持局部排版）
CAPABILITY = {
    "replace": {"whole", "section", "elements"},
    "edit": {"whole", "section", "elements"},
    "rewrite": {"whole", "section", "elements"},
    "format": {"whole"},
}

ALL_CONTAINERS = frozenset({"body", "table", "header", "footer"})

#旧四枚举到新动作的映射：patch/global 改名，避免与范围词（whole/global）混淆
MODE_TO_ACTION = {
    "replace": "replace",
    "patch": "edit",
    "global": "rewrite",
    "format": "format",
}

#标题归一化时允许剥离的固定后缀，只做确定性处理，不做同义改写
_SCOPE_SUFFIXES = ("章节正文", "章节", "部分", "正文", "小节")


class Scope(BaseModel):
    """一次改写的作用范围。字段由模型填写，代码只校验不解释。"""

    face: Literal["whole", "section", "elements"] = Field(
        description="whole=整篇；section=指定章节；elements=用户明确给出元素下标区间。"
    )
    section_title: Optional[str] = Field(
        default=None,
        description=(
            "face=section 时必填。必须逐字取自【文档大纲】；"
            "用户点名的章节不存在时，照用户原话填写，不要改成别的章节。"
        ),
    )
    element_range: Optional[List[int]] = Field(
        default=None,
        description="face=elements 时必填，[起,止] 含两端。",
    )
    containers: Optional[List[Container]] = Field(
        default=None,
        description=(
            "本次允许改动的容器。用户说'正文'就只填 ['body']；提到表格就加 'table'；"
            "没提容器时留空（整篇=全容器，章节/区间=仅正文）。"
        ),
    )
    basis_span: str = Field(
        default="",
        description="用户在指令中支撑该范围的原文片段，必须逐字摘录，不得改写、不得跨句拼接。",
    )
    note: str = Field(default="", description="范围判断依据，一句话，仅用于日志。")


def normalize_scope_title(text: Any) -> str:
    """只做确定性归一化：NFKC、去全部空白、剥离固定后缀。"""
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")).strip())
    for suffix in _SCOPE_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def normalize_span(text: Any) -> str:
    """依据片段只去空白并做 NFKC，保留原始用词。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def is_grounded(span: Any, user_input: Any) -> bool:
    """依据片段是否逐字出现在用户原话中（允许空白/全半角差异）。"""
    value = normalize_span(span)
    return bool(value) and value in normalize_span(user_input)


def effective_containers(scope: Optional[Scope]) -> set:
    """容器的有效值：为空时按保守默认，整篇=全容器，章节/区间=仅正文。"""
    if scope is None:
        return set(ALL_CONTAINERS)
    if scope.containers:
        return set(scope.containers)
    return set(ALL_CONTAINERS) if scope.face == "whole" else {"body"}


def outline_index(entries) -> Dict[str, Any]:
    """把大纲树压成“归一化标题 -> 区间”的索引，供校验与范围解析共用。"""
    titles: List[str] = []
    ranges: Dict[str, Tuple[int, int]] = {}
    range_titles: Dict[Tuple[int, int], str] = {}

    def _walk(items):
        for entry in items or []:
            title = (entry.get("title") or "").strip()
            index_range = entry.get("index_range") or []
            if title and len(index_range) == 2:
                start, end = int(index_range[0]), int(index_range[1])
                titles.append(title)
                key = normalize_scope_title(title)
                old = ranges.get(key)
                #同名标题取范围更小的（更具体），与现有大纲匹配习惯一致
                if old is None or (end - start) < (old[1] - old[0]):
                    ranges[key] = (start, end)
                range_titles[(start, end)] = title
            _walk(entry.get("children") or [])

    _walk(entries)
    return {"titles": titles, "ranges": ranges, "range_titles": range_titles}


def resolve_range(scope: Optional[Scope], outline: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """把 scope 解析成元素下标区间；返回 None 表示整篇。"""
    if scope is None or scope.face == "whole":
        return None
    if scope.face == "section":
        return (outline.get("ranges") or {}).get(normalize_scope_title(scope.section_title))
    element_range = scope.element_range or []
    if len(element_range) != 2:
        return None
    start, end = min(element_range), max(element_range)
    return start, end


def title_for_range(outline: Dict[str, Any], index_range: Optional[Tuple[int, int]]) -> str:
    if not index_range:
        return ""
    return (outline.get("range_titles") or {}).get(tuple(index_range), "")


def validate_scope(action: str, scope: Optional[Scope], outline: Dict[str, Any],
                   user_input: str, total_elements: int,
                   require_grounding: bool = True) -> List[str]:
    """只查事实：依据在不在原话、章节在不在大纲、区间越不越界、动作支不支持该范围。"""
    errs: List[str] = []
    if scope is None:
        return ["E_SCOPE_MISSING: 缺少范围契约"]

    if require_grounding and not is_grounded(scope.basis_span, user_input):
        errs.append("E_SCOPE_UNGROUNDED: 范围依据未能逐字取自用户指令")

    if scope.face == "section":
        title = normalize_scope_title(scope.section_title)
        if not title:
            errs.append("E_SCOPE_SECTION_MISSING: face=section 必须给出章节标题")
        elif title not in (outline.get("ranges") or {}):
            existing = "、".join(outline.get("titles") or []) or "（无）"
            errs.append(f"E_SCOPE_SECTION_NOT_FOUND: 大纲中没有章节「{scope.section_title}」，"
                        f"现有：{existing}")

    if scope.face == "elements":
        element_range = scope.element_range or []
        if len(element_range) != 2:
            errs.append("E_SCOPE_RANGE_MISSING: face=elements 必须给出 [起,止] 区间")
        else:
            start, end = min(element_range), max(element_range)
            if not (0 <= start <= end < total_elements):
                errs.append(f"E_SCOPE_RANGE_INVALID: 元素区间 {element_range} 越界"
                            f"（共 {total_elements} 个元素）")

    if scope.face not in CAPABILITY.get(action, set()):
        errs.append(f"E_CAPABILITY: {action} 暂不支持 {scope.face} 范围")

    if action == "replace":
        containers = effective_containers(scope)
        if scope.face != "whole" and containers & {"header", "footer"}:
            errs.append("E_CAPABILITY: 页眉/页脚无法按章节或元素区间限定，"
                        "如需同时改整篇页眉页脚，请拆成 face=whole 的独立步骤")
        if scope.face == "whole":
            if "body" not in containers:
                errs.append("E_CAPABILITY: 整篇范围必须包含正文")
            if scope.containers and containers != set(ALL_CONTAINERS):
                errs.append("E_CAPABILITY: 本版本整篇 replace 暂不支持容器收窄（随 DEF-001 提供）")

    return errs


def _is_short_span(span: Any, min_chars: int = 4) -> bool:
    return 0 < len(normalize_span(span)) < min_chars


def lint_scope(action: str, scope: Optional[Scope], user_input: str) -> List[str]:
    """语义可疑只告警，不改变行为。"""
    warns: List[str] = []
    if scope is None:
        return warns
    if action != "replace" and scope.containers is not None:
        warns.append("containers 只对 replace 生效，本步将按动作默认范围执行")
    if scope.face != "whole" and scope.containers and not any(
            word in normalize_span(scope.basis_span) for word in ("正文", "表格", "页眉", "页脚")):
        warns.append("容器为子集，但依据片段未提及容器，请复核模型判断")
    if _is_short_span(scope.basis_span):
        warns.append("依据片段过短，建议复核范围是否说清楚")
    return warns


def scope_key(scope: Optional[Scope]) -> Tuple:
    """合并步骤用的范围键：同键才允许合并，不同范围绝不合并。"""
    containers = tuple(sorted(effective_containers(scope)))
    if scope is None or scope.face == "whole":
        return ("whole", containers)
    if scope.face == "section":
        return ("section", normalize_scope_title(scope.section_title), containers)
    element_range = scope.element_range or []
    ident = (min(element_range), max(element_range)) if len(element_range) == 2 else ()
    return ("elements", ident, containers)


def scope_label(scope: Optional[Scope]) -> str:
    """日志用的范围描述。"""
    if scope is None or scope.face == "whole":
        return "whole"
    if scope.face == "section":
        return f"section:{scope.section_title or ''}"
    element_range = scope.element_range or []
    if len(element_range) == 2:
        return f"elements:{min(element_range)}-{max(element_range)}"
    return "elements"


def upgrade_step(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把旧 mode/target 形态或新 action/scope 形态统一成一套步骤字典。"""
    step = dict(raw or {})
    action = str(step.get("action") or "").strip().lower()
    if action not in CAPABILITY:
        action = MODE_TO_ACTION.get(str(step.get("mode") or "").strip().lower(), "")

    scope = step.get("scope")
    scope_invalid = False
    if isinstance(scope, Scope):
        pass
    elif isinstance(scope, dict):
        try:
            scope = Scope.model_validate(scope)
        except Exception:
            #格式非法不能静默退回整篇：标记出来，由执行层拒绝
            scope_invalid = True
            scope = None
    elif scope is not None:
        scope_invalid = True
        scope = None

    return {
        "order": step.get("order", 1),
        "action": action,
        "scope": scope,
        "scope_invalid": scope_invalid,
        "target": str(step.get("target") or "").strip(),
        "target_range": step.get("target_range"),
        "instruction": str(step.get("instruction") or "").strip(),
        "replace_pairs": dict(step.get("replace_pairs") or {}),
    }
