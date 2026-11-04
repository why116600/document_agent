"""字段-事实计划：章节优先，字段映射到章节，事实带作用域。

方案要点：
- 章节由原规划器决定，字段计划只做内容映射，不覆盖章节结构；
- 事实身份是 (text, scope)，同一文本不同作用域允许跨字段；
- 只有同文本同作用域跨字段才判重复；
- 显式 allow 规则必须 explicit=True 且有 reason，才允许跨字段重复；
- 模型不可用/计划失败时返回 None，调用方回退旧路径。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, List, Optional, Sequence

from pydantic import BaseModel, Field, field_validator

from llm_client import llm_model_invoke


class ScopedFact(BaseModel):
    text: str = ""
    scope: str = ""

    @field_validator("text", mode="before")
    @classmethod
    def _coerce_text(cls, value):
        return str(value or "").strip()


class FieldFact(BaseModel):
    field: str = ""
    chapter: str = ""
    facts: List[ScopedFact] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def _coerce_facts(cls, value):
        facts = []
        for item in value or []:
            if isinstance(item, str):
                facts.append({"text": item, "scope": ""})
            elif isinstance(item, dict):
                facts.append(item)
            elif isinstance(item, ScopedFact):
                facts.append(item)
        return facts


class AllowedRepeat(BaseModel):
    fact: str = ""
    fields: List[str] = Field(default_factory=list)
    reason: str = ""
    explicit: bool = False


class DocumentPlan(BaseModel):
    chapters: List[str] = Field(default_factory=list)
    fields: List[FieldFact] = Field(default_factory=list)
    allowed_repeats: List[AllowedRepeat] = Field(default_factory=list)


# 兼容旧测试/旧调用：FieldPlan 仍可构造，但新流程使用 DocumentPlan。
FieldPlan = DocumentPlan

_MISSING_MARKERS = {"资料未说明", "资料未提供", "未说明", "未提供", "无"}
_TEST_METADATA_RE = re.compile(
    r"(?:语料标识|用例编号|测试标识|CP-\d+|虚构测试材料)", re.I
)


def normalize_text(text: Any) -> str:
    """去掉空白和常见标点，供事实/章节字符串匹配。"""
    return re.sub(
        r"[\s，。；、：！？（）【】《》“”‘’\-—,.;:!?()\[\]{}\"'`]+",
        "",
        str(text or ""),
    ).lower()


def _is_missing_fact(text: str) -> bool:
    return normalize_text(text) in {normalize_text(item) for item in _MISSING_MARKERS}


def is_missing_fact(text: str) -> bool:
    return _is_missing_fact(text)


def _clean_facts(items, field_name: str = "") -> List[ScopedFact]:
    facts = []
    seen = set()
    for item in items or []:
        if isinstance(item, str):
            text, scope = item.strip(), ""
        elif isinstance(item, ScopedFact):
            text, scope = item.text.strip(), item.scope.strip()
        elif isinstance(item, dict):
            text, scope = str(item.get("text") or "").strip(), str(item.get("scope") or "").strip()
        else:
            continue
        if not text or _TEST_METADATA_RE.search(text):
            continue
        if field_name and text.startswith(field_name):
            stripped = text[len(field_name):].lstrip("：: ")
            if stripped:
                text = stripped
        key = (normalize_text(text), normalize_text(scope))
        if key in seen:
            continue
        seen.add(key)
        facts.append(ScopedFact(text=text, scope=scope))
    if len(facts) > 1:
        facts = [
            fact for fact in facts
            if not any(
                normalize_text(fact.text) != normalize_text(other.text)
                and normalize_text(fact.text) in normalize_text(other.text)
                for other in facts
            )
        ]
    return facts or [ScopedFact(text="资料未说明", scope="")]


def map_field_chapter(field: str, chapter: str, chapters: Sequence[str]) -> str:
    """把字段映射到已有章节；支持“内部演示限制”映射到“使用性质与内部演示限制”。"""
    chapter_by_norm = {normalize_text(item): item for item in chapters}
    chapter_norm = normalize_text(chapter)
    if chapter_norm in chapter_by_norm:
        return chapter_by_norm[chapter_norm]
    field_norm = normalize_text(field)
    if not field_norm:
        return ""
    candidates = [
        item for item in chapters
        if field_norm in normalize_text(item) or normalize_text(item) in field_norm
    ]
    if not candidates:
        return ""
    return min(candidates, key=lambda item: (len(normalize_text(item)), len(item)))


def build_document_plan(
    llm,
    dialog: str,
    material_block: str,
    chapters: Sequence[str],
    required_topics: Sequence[str],
) -> Optional[DocumentPlan]:
    """方案要点：字段映射到已有章节，不新增/重排章节。"""
    chapter_list = [str(item).strip() for item in (chapters or []) if str(item).strip()]
    topics = [str(item).strip() for item in (required_topics or []) if str(item).strip()]
    if not chapter_list or not topics or llm is None:
        return None
    prompt = (
        "你是文档字段规划助手。请根据用户要求和资料，把事实映射到已有章节。\n"
        f"用户要求：{dialog}\n"
        f"已有章节（不得新增、删除或重排）：{'、'.join(chapter_list)}\n"
        f"必须覆盖以下字段：{'、'.join(topics)}\n"
        "每个字段必须映射到上述某个章节；字段名必须来自上述字段；"
        "同类且资料未说明的字段可以合并为一个字段。\n"
        "每个事实只能归一个字段；同一事实同一作用域不得分给多个字段；"
        "同一文本但不同作用域可以分别保留。\n"
        "facts 必须带 scope；scope 说明适用对象/范围/版本/新旧关系；"
        "facts 使用资料原句或短语，保留数字、单位、角色、动作、期限和限定语；"
        "资料没有的字段写“资料未说明”。\n"
        "只有用户明确允许跨字段重复时，才输出 allowed_repeats，且必须 explicit=true 并给出 reason；"
        "否则不要输出 allowed_repeats。\n"
        "不要写入语料标识、CP-编号、文件名等测试元数据。\n"
        f"{material_block}\n"
        "只输出 DocumentPlan JSON。"
    )
    try:
        plan = llm_model_invoke(llm, prompt, DocumentPlan)
    except Exception:
        return None
    if not isinstance(plan, DocumentPlan) or not plan.fields:
        return None

    chapter_by_norm = {normalize_text(item): item for item in chapter_list}
    fields = []
    seen_fields = set()
    for item in plan.fields:
        field_name = str(item.field or "").strip()
        if not field_name or field_name in seen_fields:
            continue
        seen_fields.add(field_name)
        chapter = str(item.chapter or "").strip()
        if normalize_text(chapter) not in chapter_by_norm:
            chapter = map_field_chapter(field_name, chapter, chapter_list)
        fields.append(FieldFact(
            field=field_name,
            chapter=chapter,
            facts=_clean_facts(item.facts, field_name),
        ))

    allowed = []
    for rule in plan.allowed_repeats or []:
        if not rule.explicit or not str(rule.reason or "").strip():
            continue
        allowed.append(AllowedRepeat(
            fact=str(rule.fact or "").strip(),
            fields=[str(field).strip() for field in rule.fields if str(field).strip()],
            reason=str(rule.reason).strip(),
            explicit=True,
        ))
    return DocumentPlan(
        chapters=chapter_list, fields=fields, allowed_repeats=allowed
    ) if fields else None


def build_field_plan(llm, dialog: str, material_block: str, required_topics: Sequence[str]):
    """兼容旧调用：没有章节时，用字段名作为临时章节。"""
    topics = [str(item).strip() for item in (required_topics or []) if str(item).strip()]
    return build_document_plan(llm, dialog, material_block, topics, topics)


def fields_for_chapter(plan: Optional[DocumentPlan], chapter: str) -> List[FieldFact]:
    if plan is None:
        return []
    chapter_norm = normalize_text(chapter)
    result = []
    for item in plan.fields:
        mapped = normalize_text(item.chapter)
        if mapped == chapter_norm:
            result.append(item)
        elif not mapped and normalize_text(item.field) == chapter_norm:
            result.append(item)
    return result


def field_plan_block(plan: Optional[DocumentPlan], only_chapter: str = "") -> str:
    if plan is None:
        return ""
    lines = ["【字段-事实计划】"]
    for item in plan.fields:
        if only_chapter and normalize_text(item.chapter) != normalize_text(only_chapter):
            continue
        if not item.facts:
            continue
        facts = "；".join(
            f"{fact.text}（作用域：{fact.scope}）" if fact.scope else fact.text
            for fact in item.facts
        )
        lines.append(f"- {item.chapter or '未映射章节'} / {item.field}：{facts}")
    lines.append("同一事实同一作用域只允许出现在一个字段；不同作用域可以分别保留。")
    return "\n".join(lines) + "\n"


def _chapter_blocks(doc, chapters: Sequence[str]):
    chapter_names = {normalize_text(item): item for item in chapters}
    blocks = {item: [] for item in chapters}
    seen_headings = []
    current = ""
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        normalized = normalize_text(text)
        if normalized in chapter_names:
            current = chapter_names[normalized]
            seen_headings.append(current)
            continue
        style_name = getattr(paragraph.style, "name", "")
        if style_name == "Heading 1":
            current = ""
            continue
        if style_name.startswith("Heading"):
            if current:
                blocks[current].append(text)
            continue
        if current:
            blocks[current].append(text)
    return blocks, seen_headings


def _fact_match_count(text: str, fact_norm: str) -> int:
    """精确包含优先；数值事实按数字+单位兜底；较长事实允许分句级近似。"""
    text_norm = normalize_text(text)
    if fact_norm in text_norm:
        return text_norm.count(fact_norm)
    numeric_tokens = re.findall(
        r"\d+(?:\.\d+)?(?:个工作日|工作日|小时|分钟|秒|天|套|台|组|次|页|年|月|日|%)",
        fact_norm,
    )
    if numeric_tokens:
        count = 0
        for token in numeric_tokens:
            count += len(re.findall(rf"(?<![\d.]){re.escape(token)}", text_norm))
        if count:
            return count
    if len(fact_norm) < 6:
        return 0
    count = 0
    for chunk in re.split(r"[，。；;、\n]", str(text or "")):
        chunk_norm = normalize_text(chunk)
        if not chunk_norm:
            continue
        if SequenceMatcher(None, fact_norm, chunk_norm).ratio() >= 0.8:
            count += 1
    return count


def validate_document_plan(
    doc,
    plan: Optional[DocumentPlan],
    chapters: Optional[Sequence[str]] = None,
) -> List[str]:
    """方案要点：章节优先 + 作用域事实校验。"""
    if plan is None:
        return []
    chapter_list = [
        str(item).strip() for item in (chapters or plan.chapters or [])
        if str(item).strip()
    ]
    blocks, seen_headings = _chapter_blocks(doc, chapter_list)
    issues = []

    for chapter in chapter_list:
        if chapter not in seen_headings:
            issues.append(f"chapter_missing:{chapter}")
    if seen_headings != [item for item in chapter_list if item in seen_headings]:
        issues.append("chapter_order")

    valid_fields = {item.field for item in plan.fields}
    valid_chapters = {normalize_text(item) for item in chapter_list}
    chapter_by_norm = {normalize_text(item): item for item in chapter_list}
    allowed_rules = []
    for rule in plan.allowed_repeats:
        if not rule.explicit or not rule.reason.strip():
            issues.append(f"invalid_allow_rule:{rule.fact}")
            continue
        if not rule.fields or any(field not in valid_fields for field in rule.fields):
            issues.append(f"invalid_allow_rule:{rule.fact}")
            continue
        allowed_rules.append(rule)

    text_fields = {}
    for item in plan.fields:
        for fact in item.facts:
            key = normalize_text(fact.text)
            text_fields.setdefault(key, []).append((item, fact))

    for key, entries in text_fields.items():
        scopes = {normalize_text(fact.scope) for _, fact in entries}
        allow_for_text = any(
            normalize_text(rule.fact) == key
            and all(item.field in rule.fields for item, _ in entries)
            for rule in allowed_rules
        )
        if (
            len(entries) > 1
            and (not any(scopes) or len(scopes) < len(entries))
            and not allow_for_text
        ):
            issues.append(f"missing_scope:{entries[0][1].text}")

    for item in plan.fields:
        field_chapter = map_field_chapter(item.field, item.chapter, chapter_list)
        if not field_chapter:
            issues.append(f"field_chapter_missing:{item.field}")
            continue
        target = blocks.get(field_chapter, [])
        for fact in item.facts:
            fact_text = fact.text.strip()
            fact_norm = normalize_text(fact_text)
            if not fact_norm or _is_missing_fact(fact_text):
                continue
            target_count = sum(
                _fact_match_count(text, fact_norm) for text in target
            )
            if target_count == 0:
                issues.append(f"field_fact_missing:{item.field}:{fact_text}")
                continue
            allowed = any(
                normalize_text(rule.fact) == fact_norm and item.field in rule.fields
                for rule in allowed_rules
            )
            if target_count > 1 and not allowed:
                issues.append(f"field_fact_duplicate:{fact_text}@{item.field}")

            entries = text_fields.get(fact_norm, [])
            distinct_scopes = {normalize_text(f.scope) for _, f in entries if f.scope}
            legal_scope_repeat = len(distinct_scopes) > 1
            other_chapters = []
            for chapter, paragraphs in blocks.items():
                if chapter == field_chapter:
                    continue
                if any(_fact_match_count(text, fact_norm) for text in paragraphs):
                    other_chapters.append(chapter)
            if other_chapters and not legal_scope_repeat and not allowed:
                issues.append(
                    f"field_fact_misplaced:{fact_text}@{'/'.join(sorted(other_chapters))}"
                )
    return sorted(set(issues))


def validate_field_plan(doc, plan: Optional[DocumentPlan]) -> List[str]:
    """兼容旧调用：没有显式章节时，用字段名作为章节。"""
    if plan is None:
        return []
    chapters = plan.chapters or [item.field for item in plan.fields]
    return validate_document_plan(doc, plan, chapters)
