"""写作素材收集：把检索要点与文件摘要整理成可直接交给写作节点的文字块。

本模块只做搬运与格式化，不做语义判断：
- end 的 arguments 是自由字典（模型可能用 query / retrieved_fields 等任意键），
  因此按"扁平化所有标量值"处理，不匹配键名；
- 不判断哪句是事实、哪句是检索指令，交由写作提示词的来源约束处理。
"""
from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

MAX_PER_FILE_CHARS = 3000     # 单份摘要最多注入 3000 字，防止提示词过长
MAX_TOTAL_CHARS = 8000        # 兜底素材总量上限
MIN_ITEM_CHARS = 2            # 太短的值（如单个标点）不要

GUARD_LINE = (
    "（以上为参考资料内容：若与用户指定来源不一致，以用户指定来源为准；"
    "没有来源支撑的数值、期限、人名或角色一律不得编造，无法确定时应如实说明缺失。）"
)


def _flatten(value: Any, out: List[str], depth: int = 0) -> None:
    """把任意嵌套结构里的文字/数字摊平成一行行文字；True/False 不算信息，跳过。"""
    if depth > 3 or value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= MIN_ITEM_CHARS:
            out.append(text)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        out.append(str(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            _flatten(item, out, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _flatten(item, out, depth + 1)


def harvest_end_arguments(arguments: Mapping[str, Any] | None) -> List[str]:
    """把"结束检索"时给出的参数里的要点全拿出来，重复的只留一份。"""
    scalars: List[str] = []
    _flatten(arguments or {}, scalars)
    seen, uniq = set(), []
    for text in scalars:
        if text not in seen:
            seen.add(text)
            uniq.append(text)
    return uniq


def build_end_material(arguments: Mapping[str, Any] | None) -> str:
    """打包成一个"要点块"；一条都没有时返回空串（好让下游走兜底）。"""
    items = harvest_end_arguments(arguments)
    if not items:
        return ""
    return "【检索结束要点】\n" + "\n".join(f"- {text}" for text in items)


def summaries_as_material(file_summaries: Mapping[str, str] | None) -> List[str]:
    """把文件摘要整理成块；每份截断，总量严格不超过 MAX_TOTAL_CHARS。"""
    blocks: List[str] = []
    used = 0
    for path, summary in (file_summaries or {}).items():
        text = str(summary or "").strip()
        if not text:
            continue
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        header = f"【参考资料摘要：{name}】\n"
        # 先扣掉块头长度，再决定正文能放多少，保证总量是硬上限而非"接近上限"
        remain = MAX_TOTAL_CHARS - used - len(header)
        if remain <= 0:
            break
        piece = text[: min(MAX_PER_FILE_CHARS, remain)]
        block = header + piece
        blocks.append(block)
        used += len(block)
    return blocks


def collect_writing_material(state: Mapping[str, Any]) -> Tuple[List[str], str]:
    """决定这次能用哪些资料：优先检索要点，没有才用摘要。返回 (资料, 来源标记)。"""
    retrieved = [str(item) for item in (state.get("retrieved_content") or []) if str(item).strip()]
    if retrieved:
        return retrieved, "retrieved"
    summaries = summaries_as_material(state.get("file_summaries") or {})
    if summaries:
        return summaries, "summary"
    return [], "empty"


def build_material_block(items: Sequence[str], source: str, title: str = "目前已知：") -> str:
    """拼成提示词里的一段；用摘要兜底时会明说"这是摘要，不是原文"。"""
    if not items:
        return ""
    head = title
    if source == "summary":
        head += "（检索阶段未产出要点，以下为参考资料摘要，细节以原文为准）"
    return f"{head}\n" + "\n".join(items) + "\n" + GUARD_LINE + "\n"
