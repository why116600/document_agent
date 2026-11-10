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
from typing import Any, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field, field_validator

from llm_client import llm_model_invoke


class ScopedFact(BaseModel):
    text: str = ""
    scope: str = ""
    # 事实身份：计划构建时由程序分配，用于账本和溯源；模型不负责生成。
    fact_id: str = ""

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


class VerifiedIssue(BaseModel):
    """独立审校发现的单条问题；只接受可回指原文的高置信度问题。"""

    code: Literal[
        "unsupported_fact",
        "fact_missing",
        "fact_misplaced",
        "illegal_duplicate",
        "contradiction",
        "semantic_drift",
        "other",
    ] = "other"
    severity: Literal["high", "medium", "low"] = "high"
    fact: str = ""
    chapter: str = ""
    evidence: str = ""
    detail: str = ""


class DocumentVerification(BaseModel):
    issues: List[VerifiedIssue] = Field(default_factory=list)
    verdict: Literal["pass", "fail"] = "pass"

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


_UNIT_FACT_RE = re.compile(
    r"\d+(?:\.\d+)?(?:个工作日|工作日|小时|分钟|秒|天|套|台|组|次|页|年|月|日|%)"
)


def _has_unit_fact(text: str) -> bool:
    """是否包含“数值+单位”事实；这类事实是字段的硬性事实。"""
    return bool(_UNIT_FACT_RE.search(str(text or "")))


def required_facts(field: "FieldFact") -> List[ScopedFact]:
    """字段的必写事实：全部数值事实；没有数值事实时保留第一条。

    说明性/解释性事实不参与硬校验，只在字数允许时由写作或兜底补充。
    """
    facts = [
        fact for fact in (field.facts or [])
        if fact.text.strip() and not _is_missing_fact(fact.text)
    ]
    if not facts:
        return []
    selected = [fact for fact in facts if _has_unit_fact(fact.text)] or [facts[0]]
    deduped = []
    seen = set()
    for fact in selected:
        key = (normalize_text(fact.text), normalize_text(fact.scope))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped


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
            remainder = text[len(field_name):]
            # 只剥离“字段名：值”或“字段名 值”这类真正的标签前缀；
            # “归还时登记时间”“审批由……批准”中的字段名是句子成分，不能删。
            if remainder[:1] in ("：", ":") or remainder[:1].isspace():
                stripped = remainder.lstrip("：: \t")
                if re.search(r"[\w\u4e00-\u9fff]", stripped):
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


def _drop_cross_field_duplicates(fields: List["FieldFact"]) -> None:
    """同一事实同一作用域只保留一个字段：优先保留“只靠它支撑”的字段。"""
    groups = {}
    for field in fields:
        for fact in field.facts:
            if _is_missing_fact(fact.text):
                continue
            key = (normalize_text(fact.text), normalize_text(fact.scope))
            groups.setdefault(key, []).append((field, fact))

    drop_ids = set()
    for entries in groups.values():
        if len({id(field) for field, _ in entries}) <= 1:
            continue

        def has_other_fact(field, fact):
            return any(
                other is not fact and other.text.strip() and not _is_missing_fact(other.text)
                for other in field.facts
            )

        dependents = [field for field, fact in entries if not has_other_fact(field, fact)]
        keep_field = dependents[0] if dependents else entries[0][0]
        for field, fact in entries:
            if field is not keep_field:
                drop_ids.add(id(fact))

    if not drop_ids:
        return
    for field in fields:
        field.facts = [fact for fact in field.facts if id(fact) not in drop_ids]


def _remove_embedded_text(long_text: str, short_text: str) -> str:
    """从较长事实中删除已经被独立字段承载的短事实，连同相邻分隔符。"""
    long_value = str(long_text or "")
    short_value = str(short_text or "").strip("。；; ")
    if not short_value:
        return long_value
    index = long_value.find(short_value)
    if index < 0:
        return long_value
    start = index
    while start > 0 and long_value[start - 1] in "，,；;、 ":
        start -= 1
    end = index + len(short_value)
    while end < len(long_value) and long_value[end] in "，,；;、 ":
        end += 1
    return (long_value[:start] + long_value[end:]).strip("，,；;、 。")


def _trim_cross_field_containment(fields: List["FieldFact"]) -> None:
    """同一事实被拆分到两个字段时，从较长事实中删掉已由专有字段承载的部分。"""
    for long_field in fields:
        for long_fact in list(long_field.facts):
            long_norm = normalize_text(long_fact.text)
            for short_field in fields:
                if short_field is long_field:
                    continue
                for short_fact in short_field.facts:
                    short_norm = normalize_text(short_fact.text)
                    if len(short_norm) < 4 or short_norm not in long_norm:
                        continue
                    dedicated = (
                        len(short_field.facts) == 1
                        or normalize_text(short_field.field) in short_norm
                    )
                    if not dedicated:
                        continue
                    trimmed = _remove_embedded_text(long_fact.text, short_fact.text)
                    if (
                        trimmed
                        and normalize_text(trimmed) != long_norm
                        and len(normalize_text(trimmed)) >= 6
                    ):
                        print(
                            f"【计划修复】移除跨字段重叠事实："
                            f"{long_field.field}/{long_fact.text} -> {trimmed}"
                        )
                        long_fact.text = trimmed
                        long_norm = normalize_text(trimmed)


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


_CLAUSE_SPLIT_RE = re.compile(r"[。；;\n]")
_LEADING_CONNECTIVES = set("由在于对向给为的地得")


def _strip_leading_connectives(text: str) -> str:
    """去掉模型常补的单个连接词，仅用于来源包含判断，不用于截断恢复。"""
    value = str(text or "")
    if len(value) > 2 and value[:1] in _LEADING_CONNECTIVES:
        return value[1:]
    return value


def _strip_label_prefix(text: str, labels: Sequence[str]) -> str:
    """去掉事实开头的结构标签（来源标签/字段名/章节名 + 冒号），保留事实本身。"""
    value = str(text or "").strip()
    for label in labels:
        label = str(label or "").strip()
        if not label or not value.startswith(label):
            continue
        remainder = value[len(label):]
        if remainder[:1] in ("：", ":") or remainder[:1].isspace():
            stripped = remainder.lstrip("：: \t")
            if re.search(r"[\w\u4e00-\u9fff]", stripped):
                return stripped
    # 来源资料本身常带“仪器配置：/适用范围：/记录保存：”这类短标签。
    match = re.match(r"^([^：:，。；;\s]{2,10})[：:]\s*(.+)$", value)
    if match and re.search(r"[\w\u4e00-\u9fff]", match.group(2)):
        return match.group(2).strip()
    return value


def restore_truncated_fact(fact_text: str, field_name: str, source_text: str) -> str:
    """若事实只是源句的后半段、且被截掉的部分包含字段名，则恢复完整源句。

    典型场景：field=归还，fact=时登记时间，源句=归还时登记时间。
    这是确定性修复，不依赖模型重写。
    """
    fact_norm = normalize_text(fact_text)
    field_norm = normalize_text(field_name)
    if not fact_norm or not field_norm or len(normalize_text(source_text)) < 20:
        return fact_text
    for clause in _CLAUSE_SPLIT_RE.split(str(source_text or "")):
        clause = clause.strip()
        # 带冒号的子句是“标签：值”，属于结构标签，不是被截断的原句。
        if "：" in clause or ":" in clause:
            continue
        clause_norm = normalize_text(clause)
        index = clause_norm.find(fact_norm)
        if index <= 0:
            continue
        prefix = clause_norm[:index]
        if prefix.endswith(field_norm) or (
            field_norm in prefix and len(prefix) <= len(field_norm) + 2
        ):
            restored = clause.strip("。；; ")
            if normalize_text(restored) != fact_norm:
                return restored
    return fact_text


def source_fidelity_warnings(plan: Optional[DocumentPlan],
                             source_text: str) -> List[str]:
    """告警：必写事实在资料中找不到依据。只告警，不阻断，交由独立审校兜底。"""
    if plan is None or len(normalize_text(source_text)) < 20:
        return []
    source_norm = normalize_text(source_text)
    warnings = []
    for field in plan.fields:
        for fact in required_facts(field):
            fact_norm = normalize_text(fact.text)
            if not fact_norm or _is_missing_fact(fact.text):
                continue
            probe = _strip_leading_connectives(fact_norm)
            if fact_norm not in source_norm and probe not in source_norm:
                warnings.append(f"{field.field}/{fact.text}")
    return warnings[:8]


def uncovered_topics(plan: Optional[DocumentPlan],
                     required_topics: Sequence[str]) -> List[str]:
    """用户显式要求但计划里没有对应字段的主题；用于告警。"""
    if plan is None:
        return []
    names = [normalize_text(field.field) for field in plan.fields if field.field]
    result = []
    for topic in required_topics or []:
        topic_norm = normalize_text(topic)
        if not topic_norm:
            continue
        if not any(topic_norm in name or name in topic_norm for name in names):
            result.append(str(topic))
    return result


def estimate_min_body_chars(plan: Optional[DocumentPlan],
                            include_section_titles: bool = True,
                            include_field_names: bool = False) -> int:
    """必写事实 + 结构标题 + 字段标签的最短长度估算，用于生成前可行性预检。"""
    if plan is None:
        return 0
    total = 0
    for field in plan.fields:
        facts = required_facts(field)
        if not facts:
            continue
        total += sum(len(fact.text.strip()) for fact in facts)
        total += 2 * max(len(facts) - 1, 0)
        if include_field_names:
            total += len(field.field.strip()) + 1
    if include_section_titles:
        total += sum(len(chapter) for chapter in plan.chapters)
    return total


def fact_key(field_index: int, fact_index: int, fact: ScopedFact) -> str:
    return fact.fact_id or f"f{field_index}-{fact_index}"


def matching_fact_keys(plan: Optional[DocumentPlan], text: str) -> set:
    """返回当前文本已经写到的计划事实 id；用于章节间事实账本。"""
    keys = set()
    if plan is None or not text:
        return keys
    for field_index, field in enumerate(plan.fields):
        for fact_index, fact in enumerate(field.facts):
            if _is_missing_fact(fact.text) or not fact.text.strip():
                continue
            if _fact_match_count(text, normalize_text(fact.text)) > 0:
                keys.add(fact_key(field_index, fact_index, fact))
    return keys


def fact_ledger_block(plan: Optional[DocumentPlan], written_keys: set,
                      max_facts: int = 20) -> str:
    """把已写事实注入后续章节；同一作用域不得复述，不同作用域仍可保留。"""
    if plan is None or not written_keys:
        return ""
    lines = ["【已写事实账本】"]
    count = 0
    for field_index, field in enumerate(plan.fields):
        for fact_index, fact in enumerate(field.facts):
            if fact_key(field_index, fact_index, fact) not in written_keys:
                continue
            if _is_missing_fact(fact.text) or not fact.text.strip():
                continue
            scope = fact.scope.strip() or "未标注作用域"
            lines.append(f"- {fact.text.strip()}（作用域：{scope}）")
            count += 1
            if count >= max_facts:
                break
        if count >= max_facts:
            break
    if len(lines) == 1:
        return ""
    lines.append("以上事实同一作用域不得在后续章节复述；不同作用域可以分别保留。")
    return "\n".join(lines) + "\n"


def verify_document(llm, dialog: str, material_block: str,
                    plan: Optional[DocumentPlan], doc_text: str,
                    required_topics: Sequence[str] = ()) -> Optional[DocumentVerification]:
    """独立事实审校：只判断源文支持性，不评价文风、字数或格式。"""
    if llm is None or plan is None or not doc_text.strip():
        return None
    topics = "、".join(str(item).strip() for item in required_topics or [] if str(item).strip())
    prompt = (
        "你是独立事实审校员，不参与写作。请逐条核对成稿是否被给定资料支持。\n"
        f"用户要求：{dialog}\n"
        f"用户显式要求覆盖的字段：{topics or '未指定'}\n"
        f"{field_plan_block(plan)}"
        f"资料：\n{material_block}\n"
        f"成稿：\n{doc_text}\n"
        "只报告以下高置信度问题，且每条必须给出资料原文作为 evidence：\n"
        "1 unsupported_fact：成稿中的事实在资料里找不到依据；\n"
        "2 fact_missing：字段计划中的必写事实未出现在成稿；\n"
        "3 fact_misplaced：事实出现在错误章节；\n"
        "4 illegal_duplicate：同一事实同一作用域在多个字段/章节重复；\n"
        "5 contradiction：成稿与资料矛盾（对象、角色、单位、否定、起止点）；\n"
        "6 semantic_drift：成稿改变了原意。\n"
        "不要报告文风、字数、格式、标点问题；不确定就不要报告；同一问题只报一次。\n"
        "只输出 DocumentVerification JSON。"
    )
    try:
        result = llm_model_invoke(llm, prompt, DocumentVerification)
    except Exception:
        return None
    if not isinstance(result, DocumentVerification):
        return None
    return result


_VERIFY_CODE_MAP = {
    "fact_missing": "verify_fact_missing",
    "fact_misplaced": "verify_fact_misplaced",
    "illegal_duplicate": "verify_illegal_duplicate",
    "unsupported_fact": "verify_unsupported_fact",
    "contradiction": "verify_contradiction",
    "semantic_drift": "verify_semantic_drift",
}


def verification_issue_strings(report: Optional[DocumentVerification]) -> List[str]:
    """把审校报告转成现有修复链可识别的 issue 字符串；低置信度只记录不阻断。"""
    if report is None:
        return []
    issues = []
    for item in report.issues:
        if item.severity == "low":
            continue
        fact = item.fact.strip() or item.detail.strip() or item.evidence.strip()
        prefix = _VERIFY_CODE_MAP.get(item.code, "verify_other")
        chapter = item.chapter.strip()
        if item.code == "fact_missing":
            issues.append(f"{prefix}:{chapter or '-'}:{fact}")
        elif item.code == "fact_misplaced":
            issues.append(f"{prefix}:{fact}@{chapter or '-'}")
        else:
            issues.append(f"{prefix}:{fact}")
    return list(dict.fromkeys(issues))


def build_document_plan(
    llm,
    dialog: str,
    material_block: str,
    chapters: Sequence[str],
    required_topics: Sequence[str],
    source_text: str = "",
) -> Optional[DocumentPlan]:
    """方案要点：字段映射到已有章节，不新增/重排章节。"""
    chapter_list = [str(item).strip() for item in (chapters or []) if str(item).strip()]
    topics = [str(item).strip() for item in (required_topics or []) if str(item).strip()]
    if not chapter_list or not topics or llm is None:
        return None
    source = source_text or material_block
    prompt = (
        "你是文档字段规划助手。请根据用户要求和资料，把事实映射到已有章节。\n"
        f"用户要求：{dialog}\n"
        f"已有章节（不得新增、删除或重排）：{'、'.join(chapter_list)}\n"
        f"必须覆盖以下字段：{'、'.join(topics)}\n"
        "每个字段必须映射到上述某个章节；字段名必须来自上述字段；"
        "同类且资料未说明的字段可以合并为一个字段。\n"
        "时间/期限类字段优先承载单次时长、有效期等时间事实；"
        "适用条件/限制类字段承载范围、条件；不要把时间事实归入适用范围/限制字段。\n"
        "每个事实只能归一个字段；同一事实同一作用域不得分给多个字段；"
        "同一文本但不同作用域可以分别保留。\n"
        "facts 必须带 scope；scope 说明适用对象/范围/版本/新旧关系；"
        "facts 使用资料原句或短语，保留数字、单位、角色、动作、期限和限定语；"
        "不得截断原句，例如“归还时登记时间”不得写成“时登记时间”；"
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
        if not chapter:
            # 字段无法落到任何章节时，计划不可用；放弃计划比把事实写到错误位置更安全。
            print(f"【计划修复】字段无法映射到章节，放弃字段计划：{field_name}")
            return None
        facts = _clean_facts(item.facts, field_name)
        for fact in facts:
            before = fact.text
            fact.text = _strip_label_prefix(fact.text, (field_name, chapter))
            if fact.text != before:
                print(f"【计划修复】去除事实标签前缀：{field_name}/{before} -> {fact.text}")
            restored = restore_truncated_fact(fact.text, field_name, source)
            if restored != fact.text:
                print(f"【计划修复】事实前缀恢复：{field_name}/{fact.text} -> {restored}")
                fact.text = restored
        fields.append(FieldFact(
            field=field_name,
            chapter=chapter,
            facts=facts,
        ))

    for field_index, field in enumerate(fields):
        for fact_index, fact in enumerate(field.facts):
            fact.fact_id = f"f{field_index}-{fact_index}"

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
    if not allowed:
        _drop_cross_field_duplicates(fields)
        _trim_cross_field_containment(fields)
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
        required_ids = {id(fact) for fact in required_facts(item)}
        if not required_ids:
            required_ids = {
                id(fact) for fact in item.facts if _is_missing_fact(fact.text)
            }
        facts = "；".join(
            f"{fact.text}（作用域：{fact.scope}；{'必写' if id(fact) in required_ids else '可省略'}）"
            if fact.scope else
            f"{fact.text}（{'必写' if id(fact) in required_ids else '可省略'}）"
            for fact in item.facts
        )
        lines.append(f"- {item.chapter or '未映射章节'} / {item.field}：{facts}")
    lines.append("字段名只用于定位，不要写进正文；“必写”事实必须原样写入；"
                 "“可省略”事实只在不超字数时使用，不得挤占必写事实；"
                 "同一事实同一作用域只允许出现在一个字段，不同作用域可以分别保留。")
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
        style_name = getattr(paragraph.style, "name", "")
        # 只有真正的 Heading 1 才是章节边界；正文事实与章节同名时不能当成标题跳过。
        if style_name == "Heading 1" and normalized in chapter_names:
            current = chapter_names[normalized]
            seen_headings.append(current)
            continue
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


def _document_body_text(doc) -> str:
    """无标题形态下把正文段落和表格单元格合并为一个校验块。"""
    texts = []
    for paragraph in doc.paragraphs:
        if not getattr(paragraph.style, "name", "").startswith("Heading"):
            texts.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.append("".join(paragraph.text for paragraph in cell.paragraphs))
    return "\n".join(text for text in texts if text.strip())


def validate_document_plan(
    doc,
    plan: Optional[DocumentPlan],
    chapters: Optional[Sequence[str]] = None,
    require_chapters: bool = True,
) -> List[str]:
    """方案要点：章节优先 + 作用域事实校验；无标题形态按全文一个块校验。"""
    if plan is None:
        return []
    chapter_list = [
        str(item).strip() for item in (chapters or plan.chapters or [])
        if str(item).strip()
    ]
    blocks, seen_headings = _chapter_blocks(doc, chapter_list)
    issues = []

    if not require_chapters:
        body_text = _document_body_text(doc)
        blocks = {chapter: [body_text] for chapter in chapter_list}
        seen_headings = list(chapter_list)
    else:
        for chapter in chapter_list:
            if chapter not in seen_headings:
                issues.append(f"chapter_missing:{chapter}")
        if seen_headings != [item for item in chapter_list if item in seen_headings]:
            issues.append("chapter_order")

    valid_fields = {item.field for item in plan.fields}
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
        for fact in required_facts(item):
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

            entries = text_fields.get(fact_norm, [])
            distinct_scopes = {normalize_text(f.scope) for _, f in entries if f.scope}
            legal_scope_repeat = len(distinct_scopes) > 1
            allowed = any(
                normalize_text(rule.fact) == fact_norm and item.field in rule.fields
                for rule in allowed_rules
            )
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
