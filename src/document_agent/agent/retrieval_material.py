"""写作素材收集：把检索要点与文件摘要整理成可直接交给写作节点的文字块。

本模块只做搬运与格式化，不做语义判断：
- end 的 arguments 是自由字典（模型可能用 query / retrieved_fields 等任意键），
  因此按"扁平化所有标量值"处理，不匹配键名；
- 不判断哪句是事实、哪句是检索指令，交由写作提示词的来源约束处理。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

MAX_PER_FILE_CHARS = 3000     # 单份摘要最多注入 3000 字，防止提示词过长
MAX_TOTAL_CHARS = 8000        # 兜底素材总量上限
MIN_ITEM_CHARS = 2            # 太短的值（如单个标点）不要

GUARD_LINE = (
    "（以上为参考资料内容：若与用户指定来源不一致，以用户指定来源为准；"
    "没有来源支撑的数值、期限、人名或角色一律不得编造，无法确定时应如实说明缺失。）"
)

FIDELITY_RULES = (
    "事实保真要求：严格使用下方资料中的原句事实，保留对象、数值、单位、接收角色、"
    "起算点、适用范围、否定关系和紧邻限定语；不得省略、合并或改写限定条件；"
    "资料未提供的不得编造；无法确定时明确写“资料未说明”。"
)

# 限定语同义词组：用于生成后的保守检查，只做告警，不替代人工语义核对
_CUE_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "开始计算": ("开始计算", "起算", "发现起", "发现异常起", "从发现异常", "自发现异常"),
    "不是从": ("不是从", "而非", "非从", "而不是从"),
    "不适用于": ("不适用于", "不适用"),
    "仅适用于": ("仅适用于", "仅适用", "只适用于", "只适用"),
    "不得": ("不得", "不允许", "不能"),
    "不包括": ("不包括", "不含"),
    "例外": ("例外",),
    "最多": ("最多",),
    "至少": ("至少",),
    "不超过": ("不超过", "以内"),
}

_DEVICE_RE = re.compile(r"[A-Za-z]\d+")
_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:个工作日|工作日|小时|分钟|秒|天|套|台|组|次|页|年|月|日|%)"
)
_QUALIFIER_STARTS = ("这里", "此", "该", "上述", "其中", "同时", "但", "不是", "不得", "仅", "只")


def extract_fact_keys(text: Any) -> List[str]:
    """提取“数值+单位”事实键，如 2小时、3个工作日、40天。"""
    return [re.sub(r"\s+", "", match.group(0)) for match in _UNIT_RE.finditer(str(text or ""))]


def _read_source_text(path: str) -> str:
    """读取原始资料文本；不支持的格式返回空串，由调用方退回检索素材统计。"""
    source = Path(path)
    suffix = source.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            try:
                return source.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return source.read_text(encoding="gbk")
        if suffix == ".docx":
            from docx import Document
            doc = Document(str(source))
            parts = [paragraph.text for paragraph in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        parts.append(cell.text)
            return "\n".join(parts)
        if suffix == ".pdf":
            import pdfplumber
            with pdfplumber.open(str(source)) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return ""
    return ""


def read_source_text(paths: Sequence[str]) -> str:
    """读取全部原始资料文本，供事实保真校验和计划补料使用。"""
    texts = [_read_source_text(str(path)) for path in paths or []]
    return "\n".join(text for text in texts if text and text.strip())


def _topic_probe(topic: str) -> str:
    """取主题前两个有效字符作为检索锚点，兼容“台账保留期/借用台账”这类表述差异。"""
    value = re.sub(r"\s+", "", str(topic or ""))
    if len(value) <= 2:
        return value
    return value[:2]


def supplement_material_with_source(material_block: str, source_text: str,
                                    fact_keys: Sequence[str],
                                    topics: Sequence[str] = (),
                                    max_clauses: int = 12) -> str:
    """检索漏掉的事实或主题，从原始资料补回对应子句，避免计划误写“资料未说明”。"""
    if not source_text:
        return material_block
    compact_material = re.sub(r"\s+", "", material_block or "")
    probes = [
        re.sub(r"\s+", "", str(key))
        for key in fact_keys or []
        if str(key) and re.sub(r"\s+", "", str(key)) not in compact_material
    ]
    for topic in topics or []:
        probe = _topic_probe(topic)
        if probe and probe not in compact_material and probe not in probes:
            probes.append(probe)
    if not probes:
        return material_block
    clauses = []
    for clause in re.split(r"[。；;\n]", source_text):
        clause = clause.strip()
        if not clause:
            continue
        if any(probe in re.sub(r"\s+", "", clause) for probe in probes):
            clauses.append(clause)
        if len(clauses) >= max_clauses:
            break
    if not clauses:
        return material_block
    return (
        (material_block or "")
        + "\n【原资料补充（检索未覆盖的事实/主题）】\n"
        + "；".join(clauses)
        + "\n"
    )

def source_fact_counts_from_files(paths: Sequence[str]) -> Dict[str, int]:
    """方案要点：优先从原始资料文件统计事实，避免检索片段重复放大计数。"""
    counts: Dict[str, int] = {}
    for path in paths or []:
        text = _read_source_text(str(path))
        if not text:
            continue
        for key, count in source_fact_counts([text]).items():
            counts[key] = counts.get(key, 0) + count
    return counts


def source_fact_counts(items: Sequence[str]) -> Dict[str, int]:
    """方案要点：源感知去重——按去重后的原文句子统计事实，避免检索重复放大计数。"""
    counts: Dict[str, int] = {}
    seen_sentences = set()
    for item in items or []:
        for sentence in re.split(r"[。；;！？!\n]", str(item)):
            normalized = re.sub(r"\s+", "", sentence)
            if len(normalized) < MIN_ITEM_CHARS or normalized in seen_sentences:
                continue
            seen_sentences.add(normalized)
            for key in sorted(set(extract_fact_keys(normalized))):
                counts[key] = counts.get(key, 0) + 1
    return counts


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


def _normalize(text: Any) -> str:
    """去空白和常见标点并转小写，供保守匹配使用。"""
    return re.sub(
        r"[\s，。；、：！？（）【】《》“”‘’\-—,.;:!?()\[\]{}\"'`]+",
        "",
        str(text or ""),
    ).lower()


def _query_terms(query: Any) -> set[str]:
    """把查询拆成设备标识、数字和中文二元组，用于段落打分。"""
    text = str(query or "").lower()
    terms: set[str] = set()
    for match in re.finditer(r"[a-z]+\d+|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", text):
        token = match.group(0)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for index in range(len(token) - 1):
                terms.add(token[index:index + 2])
        else:
            terms.add(token)
    return terms


def select_source_quote(content: str, query: str, max_chars: int = 1200) -> str:
    """从原文中选出与查询最相关的段落，连同紧邻限定句返回；低置信度返回空串。

    只做保守选择：必须命中查询中的设备/对象标识和至少两个检索词；最高分并列时
    视为歧义，不返回任何段落，避免把 T1、L8 等无关设备规则混入素材。
    """
    text = str(content or "").strip()
    if not text:
        return ""
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    if not paragraphs:
        return ""
    terms = _query_terms(query)
    devices = set(_DEVICE_RE.findall(str(query or "").lower()))
    if not terms:
        return ""
    # 查询涉及期限/数量时，含具体数字的段落更可能是答案所在，给少量加权；
    # 只加在数字段落上，不改变“设备标识必须命中”的硬条件。
    numeric_query = any(
        word in str(query or "")
        for word in ("时限", "期限", "小时", "天", "保留", "频率", "数量", "工作日", "台", "套")
    )

    scored: List[Tuple[int, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        lower = paragraph.lower()
        score = sum(1 for term in terms if term in lower)
        if numeric_query:
            score += len(_UNIT_RE.findall(paragraph))
        if devices:
            if not (devices & set(_DEVICE_RE.findall(lower))):
                continue
            min_score = 2
        else:
            min_score = 3
        if score >= min_score:
            scored.append((score, index, paragraph))

    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], item[1]))
    top_score = scored[0][0]
    if len(scored) > 1 and scored[1][0] == top_score:
        return ""

    _, index, paragraph = scored[0]
    lines = [paragraph]
    if index + 1 < len(paragraphs) and paragraphs[index + 1].startswith(_QUALIFIER_STARTS):
        lines.append(paragraphs[index + 1])
    quote = "【原文摘录】\n" + "\n".join(lines)
    if len(quote) > max_chars:
        quote = quote[:max_chars]
    return quote


def extract_qualifier_clauses(material_items: Sequence[str]) -> List[str]:
    """从素材中抽取带数字/设备标识和限定标记的句子，作为生成后检查锚点。"""
    clauses: List[str] = []
    seen: set[str] = set()
    for item in material_items or []:
        for sentence in re.split(r"[。；;！？\n]", str(item or "")):
            cleaned = sentence.strip(" -\t\r")
            if not cleaned:
                continue
            if not (_DEVICE_RE.search(cleaned) or _UNIT_RE.search(cleaned)):
                continue
            has_cue = any(
                synonym in cleaned
                for synonyms in _CUE_SYNONYMS.values()
                for synonym in synonyms
            )
            if not has_cue:
                continue
            key = _normalize(cleaned)
            if key and key not in seen:
                seen.add(key)
                clauses.append(cleaned)
    return clauses


def find_missing_qualifiers(clauses: Sequence[str], generated_text: str) -> List[Dict[str, Any]]:
    """检查生成文本是否遗漏素材中的限定语；只返回疑似缺失，供告警/人工复核。"""
    generated = _normalize(generated_text)
    missing: List[Dict[str, Any]] = []
    for clause in clauses or []:
        # 优先用数字/单位锚点；只有没有数字时才退回设备标识。
        # 这样“T1 的 2 小时不适用于 E2”不会因为生成文本提到 E2 就被误判成遗漏。
        unit_anchors = list(_UNIT_RE.findall(clause))
        anchors = unit_anchors or list(_DEVICE_RE.findall(clause))
        if not anchors:
            continue
        # 该限定句锚点没有出现在生成文本中，说明这句话对应的字段未被写入，
        # 不应把“没有提到 T1/L8”误报成遗漏。
        if not any(_normalize(anchor) in generated for anchor in anchors):
            continue
        missing_cues: List[str] = []
        for cue_name, synonyms in _CUE_SYNONYMS.items():
            if not any(synonym in clause for synonym in synonyms):
                continue
            if not any(_normalize(synonym) in generated for synonym in synonyms):
                missing_cues.append(cue_name)
        if missing_cues:
            missing.append(
                {
                    "clause": clause,
                    "anchors": anchors,
                    "missing": missing_cues,
                }
            )
    return missing
