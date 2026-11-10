"""文档验收节点：在文档落盘后按 Skill 的验收项复检，输出报告但不阻断流程。

检查全部基于最终 docx 的实际内容（标题大纲级别、正文文本、引用标记）与已记录的检索证据，
因此对新建与改写两条路径都适用；验收结果写入 state['verification_report']。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import BaseModel, Field

from agent_core import AgentState
from llm_client import llm_model_invoke
from document_agent.skills.render import citation_name_pattern, citation_pattern
from document_agent.skills.schema import DocumentSkill, coerce_skill
from document_agent.skills.content_checks import normalize_heading, placeholder_hits, unsupported_assertions

PLACEHOLDER_PATTERN = re.compile(
    r"(待补充|待完善|待填写|待确认|此处省略|后续补充|TODO|TBD|XXX)", re.IGNORECASE
)
# 含这些措辞的段落属于「判断/预测」，必须带引用或显式标注无依据
CLAIM_MARKER_PATTERN = re.compile(r"(预计|预测|有望|推测|判断|将会|未来|可能)")
UNSUPPORTED_MARK = "未检索到依据"
SEMANTIC_TEXT_LIMIT = 12000
MISSING_DATA_PATTERN = re.compile(r"未提供|未披露|未给出|未获取|未检索到|未找到|缺失|暂无|无法确认|无法确定|待核实|待补充|待确认|不详|未知|未能取得|未取得|尚无|尚未|无法计算|无法获取|无法判断|无数据|无相关")
NUMBER_PATTERN = re.compile(r"\d|[零一二三四五六七八九十两][零一二三四五六七八九十百千万亿两]*(?:点[零一二三四五六七八九]+)?(?:元|%|％|个|人|家|件|倍|$)")


class SemanticIssue(BaseModel):
    rule: str = Field(description="违反的规则名称")
    detail: str = Field(description="具体问题与位置/原文片段")
    severity: Literal["high", "medium", "low"] = Field(default="medium", description="严重程度")
    kind: Literal['content', 'consistency', 'source', 'missing_requirement'] = 'content'
    quote: str = Field(default='', max_length=1200, description="逐字复制存在问题的正文片段，不得改写数值或单位")
    contradicting_quote: str = Field(default='', max_length=1200, description="一致性问题必须给出另一段逐字原文")
    source_quote: str = Field(default='', max_length=1200, description="来源问题必须给出逐字资料片段")


class RequirementAssessment(BaseModel):
    id: str
    satisfied: bool
    quote: str = Field(default='', max_length=1200)
    quotes: List[str] = Field(default_factory=list, max_length=6, description='多个独立逐字片段，不得以省略号拼接')
    detail: str = ''


class SemanticReview(BaseModel):
    passed: bool = Field(description="是否未发现语义规则违规")
    issues: List[SemanticIssue] = Field(default_factory=list, description="发现的问题列表")
    summary: str = Field(default="", description="总体评价")
    requirements: List[RequirementAssessment] = Field(default_factory=list)


class RequirementReview(BaseModel):
    requirements: List[RequirementAssessment] = Field(default_factory=list)


def _iter_blocks(doc: Document):
    """按文档顺序遍历段落与表格；低版本 python-docx 退化为只遍历段落。"""
    try:
        yield from doc.iter_inner_content()
    except AttributeError:  # pragma: no cover - 依赖已要求 python-docx>=1.2.0
        yield from doc.paragraphs


def _block_text(block: Any) -> str:
    if isinstance(block, Paragraph):
        return block.text or ""
    if isinstance(block, Table):
        parts: List[str] = []
        seen = set()
        for row in block.rows:
            for cell in row.cells:
                if cell._tc in seen:
                    continue
                seen.add(cell._tc)
                parts.append(cell.text or "")
        return "\n".join(parts)
    return ""


def heading_level(block: Any) -> Optional[int]:
    if not isinstance(block, Paragraph):
        return None
    pPr = block._element.pPr
    if pPr is not None:
        outline = pPr.find(qn("w:outlineLvl"))
        if outline is not None:
            value = outline.get(qn("w:val"))
            if value is not None and value.isdigit():
                level = int(value) + 1
                if 1 <= level <= 4:
                    return level
    style_name = getattr(block.style, "name", "") or ""
    for level in range(1, 5):
        if style_name in (f"Heading {level}", f"标题 {level}"):
            return level
    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _collect_blocks(doc: Document) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for block in _iter_blocks(doc):
        text = _block_text(block)
        blocks.append({
            "text": text,
            "normalized": _normalize(text),
            "level": heading_level(block),
            "table_rows": [[c.text for c in row.cells] for row in block.rows] if isinstance(block, Table) else [],
            "data_texts": (
                [" | ".join(cell.text for cell in row.cells) for row in block.rows]
                + [f"{header.text}：{cell.text}" for row in block.rows[1:]
                   for header, cell in zip(block.rows[0].cells, row.cells)]
                + [f"{row.cells[0].text}{header.text}：{cell.text}" for row in block.rows[1:]
                   for header, cell in zip(block.rows[0].cells[1:], row.cells[1:])]
                if isinstance(block, Table) and block.rows else [text]
            ),
        })
    return blocks


def _match_heading(blocks: List[Dict[str, Any]], title: str) -> Optional[int]:
    target = normalize_heading(title)
    if not target:
        return None
    for index, item in enumerate(blocks):
        if not item["level"]:
            continue
        text = normalize_heading(item['text'])
        if target == text:
            return index
    return None


def _section_range(blocks: List[Dict[str, Any]], start: int) -> Tuple[int, int]:
    """从某个标题开始，到下一个同级或更高级标题之前（含两端）。"""
    level = blocks[start]["level"] or 1
    end = len(blocks) - 1
    for index in range(start + 1, len(blocks)):
        current = blocks[index]["level"]
        if current and current <= level:
            end = index - 1
            break
    return start, end


def _section_text(blocks: List[Dict[str, Any]], start: int) -> str:
    start, end = _section_range(blocks, start)
    return "\n".join(blocks[index]["text"] for index in range(start, end + 1))


def _all_text(blocks: List[Dict[str, Any]]) -> str:
    return "\n".join(item["text"] for item in blocks)


def document_text(doc) -> str:
    """文档实际文本；合并单元格只读取一次。生成与验收共用该口径。"""
    return _all_text(_collect_blocks(doc))


def document_length(doc) -> int:
    return len(re.sub(r'\s+', '', document_text(doc)))


def section_issues(doc, skill, section) -> List[str]:
    """生成阶段只检查声明式章节结构，不从自由文本猜测业务事实。"""
    from document_agent.skills.schema import SectionSpec
    local = skill.model_copy() if skill else DocumentSkill(id='draft', name='draft')
    spec = SectionSpec.model_validate({**section, 'required': True})
    local.structure = local.structure.model_copy(update={'sections': [spec]})
    blocks = _collect_blocks(doc)
    checks = [_check_required_sections, _check_must_include, _check_citations, _check_no_placeholder]
    issues = []
    for check in checks:
        result = check(blocks, local)
        if not result['passed']:
            issues.append(result['detail'])
    return issues


def _check_required_sections(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    missing = []
    empty = []
    for section in skill.structure.sections:
        if not section.required:
            continue
        index = _match_heading(blocks, section.title)
        if index is None:
            missing.append(section.title)
        else:
            start, end = _section_range(blocks, index)
            body = ''.join(b['text'] for b in blocks[start + 1:end + 1] if not b['level'])
            pattern = citation_pattern(skill)
            body = pattern.sub('', body) if pattern else body
            if len(_normalize(body)) < section.min_body_chars or (body and placeholder_hits(body) and len(_normalize(body)) < 15):
                empty.append(section.title)
    return {
        "name": "required_sections",
        "passed": not missing and not empty,
        "missing_sections": missing,
        "empty_sections": empty,
        "detail": '；'.join((["缺少必填章节：" + '、'.join(missing)] if missing else []) +
                           (["章节正文为空或不足：" + '、'.join(empty)] if empty else [])) or '必填章节及正文齐全',
    }


def _check_section_order(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    positions = []
    for section in skill.structure.sections:
        index = _match_heading(blocks, section.title)
        if index is not None:
            positions.append((section.title, index))
    ordered = [index for _, index in positions]
    passed = ordered == sorted(ordered)
    if not positions:
        detail = "未匹配到任何配置章节，无法判断顺序"
    elif passed:
        detail = "章节顺序符合规范"
    else:
        detail = "章节顺序与规范不一致，实际顺序：" + " → ".join(title for title, _ in positions)
    return {"name": "section_order", "passed": passed, "detail": detail}


def _check_must_include(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    missing = []
    for section in skill.structure.sections:
        if not section.must_include:
            continue
        index = _match_heading(blocks, section.title)
        if index is None:
            if section.required:
                missing.append(f"{section.title}（章节缺失）")
            continue
        text = _normalize(_section_text(blocks, index))
        absent = [item for item in section.must_include if _normalize(item) not in text]
        if absent:
            missing.append(f"{section.title}：{'、'.join(absent)}")
    return {
        "name": "must_include",
        "passed": not missing,
        "detail": "章节要点均已覆盖" if not missing else "以下要点未出现：" + "；".join(missing),
    }


def _check_required_data(blocks, skill: DocumentSkill, fields=None) -> Dict[str, Any]:
    fields = skill.research.data_fields if fields is None else fields
    if not fields:
        return {"name": "required_data", "passed": True, "detail": "未配置必须收集的数据字段"}
    # 仅有字段名或缺失说明不算取得数据；表格按行及表头/值配对检查。
    candidates = []
    for block in blocks:
        if block.get("level"):
            continue
        for text in block.get("data_texts", [block["text"]]):
            pattern = citation_pattern(skill)
            text = pattern.sub("", text) if pattern else text
            candidates.extend(re.split(r"[。！？\n；;，,]", text))
    missing = []
    for field in fields:
        found = False
        for clause in candidates:
            normalized = _normalize(clause)
            labels = [field] + skill.research.field_aliases.get(field, [])
            label = next((_normalize(x) for x in labels if _normalize(x) in normalized), None)
            if label is None or MISSING_DATA_PATTERN.search(clause):
                continue
            value = normalized.split(label, 1)[1].strip("：:|、（）() ")
            if not value:
                continue
            if field in skill.research.numeric_fields and not NUMBER_PATTERN.search(value):
                continue
            found = True
            break
        if not found:
            missing.append(field)
    return {
        "name": "required_data",
        "passed": not missing,
        "missing_fields": missing,
        "detail": "必需字段均有内容（数值准确性仍需语义或人工核对）" if not missing else "正文缺少有效数据内容：" + "、".join(missing),
    }


def _check_citations(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    pattern = citation_pattern(skill)
    if pattern is None:
        return {"name": "citations", "passed": True, "detail": "未配置可用的引用模板，跳过引用检查"}

    global_count = sum(len(pattern.findall(item["text"])) for item in blocks)
    problems = []
    for section in skill.structure.sections:
        required_count = max(section.min_citations, 1 if section.evidence_required else 0)
        if required_count <= 0:
            continue
        index = _match_heading(blocks, section.title)
        if index is None:
            if section.required:
                problems.append(f"{section.title}（章节缺失）")
            continue
        count = len(pattern.findall(_section_text(blocks, index)))
        if count < required_count:
            problems.append(f"{section.title}（需 {required_count} 条，实际 {count} 条）")

    return {
        "name": "citations",
        "passed": not problems,
        "detail": (
            f"引用标记共 {global_count} 条，需引用章节均达标"
            if not problems else "引用不足：" + "；".join(problems)
        ),
    }


def _known_source_tokens(known_sources: Optional[List[str]]) -> set:
    tokens = set()
    for source in known_sources or []:
        text = str(source or "").strip()
        if not text:
            continue
        path = Path(text)
        tokens.add(_normalize(text))
        tokens.add(_normalize(path.name))
        if path.suffix:
            tokens.add(_normalize(path.stem))
    return {token for token in tokens if token}


def _source_matches(name: str, tokens: set, has_knowledge: bool) -> bool:
    normalized = _normalize(name)
    if not normalized:
        return False
    # 允许模型用「知识库」「知识图谱」这类泛称引用已检索到的知识库证据
    if has_knowledge and ("知识库" in normalized or "知识图谱" in normalized):
        return True
    for token in tokens:
        if token and (token in normalized or normalized in token):
            return True
    return False


def _check_citation_sources(blocks, skill: DocumentSkill, known_sources: Optional[List[str]],
                            has_knowledge: bool) -> Dict[str, Any]:
    pattern = citation_name_pattern(skill)
    if pattern is None:
        return {"name": "citation_sources", "passed": True, "detail": "未配置可用的引用模板，跳过来源对应检查"}

    names = [match.group(1).strip() for match in pattern.finditer(_all_text(blocks))]
    if not names:
        return {"name": "citation_sources", "passed": True, "detail": "正文未使用引用标记"}

    tokens = _known_source_tokens(known_sources)
    if not tokens and not has_knowledge:
        return {
            "name": "citation_sources",
            "passed": False,
            "detail": "正文存在引用，但没有任何已检索来源可供对应（禁止凭空引用）",
        }

    unmatched = [name for name in names if not _source_matches(name, tokens, has_knowledge)]
    unique_unmatched = list(dict.fromkeys(unmatched))
    return {
        "name": "citation_sources",
        "passed": not unique_unmatched,
        "detail": (
            f"引用 {len(names)} 条，均能对应到已检索来源"
            if not unique_unmatched
            else "以下引用无法对应到已检索来源：" + "、".join(unique_unmatched[:3])
        ),
    }


def _check_unsupported_claims(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    pattern = citation_pattern(skill)
    flagged: List[str] = []
    for item in blocks:
        text = item["text"].strip()
        if not text or item["level"]:
            continue
        if not unsupported_assertions(text):
            continue
        if UNSUPPORTED_MARK in text:
            continue
        if pattern is not None and pattern.search(text):
            continue
        flagged.append(text[:40])
    return {
        "name": "unsupported_claims",
        "passed": not flagged,
        "detail": (
            "含判断措辞的段落均带引用或已标注无依据"
            if not flagged
            else "以下判断缺少引用或「未检索到依据」标注：" + "；".join(flagged[:3])
        ),
    }


def _check_no_placeholder(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    hits = placeholder_hits(_all_text(blocks))
    return {
        "name": "no_placeholder",
        "passed": not hits,
        "detail": "未发现占位内容" if not hits else "发现占位内容：" + "、".join(sorted(set(hits))),
    }


def _check_min_length(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    minimum = skill.task.length_target.min_chars
    maximum = skill.task.length_target.max_chars
    if not minimum and not maximum:
        return {"name": "min_length", "passed": True, "detail": "未配置字数要求，跳过篇幅检查"}
    chars = len(re.sub(r"\s+", "", _all_text(blocks)))
    problems = []
    if minimum and chars < minimum:
        problems.append(f"少于最少字数 {minimum}")
    if maximum and chars > maximum:
        problems.append(f"超过最多字数 {maximum}")
    return {
        "name": "min_length",
        "passed": not problems,
        "detail": f"全文 {chars} 字" + ("" if not problems else "，" + "，".join(problems)),
    }


def _check_banned_expressions(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    expressions = [item for item in skill.task.banned_expressions if item]
    if not expressions:
        return {"name": "banned_expressions", "passed": True, "detail": "未配置禁用表达"}
    text = _normalize(_all_text(blocks))
    hits = [item for item in expressions if _normalize(item) in text]
    return {
        "name": "banned_expressions",
        "passed": not hits,
        "detail": "未出现禁用表达" if not hits else "出现禁用表达：" + "、".join(hits),
    }


CHECK_FUNCTIONS = {
    "required_sections": _check_required_sections,
    "section_order": _check_section_order,
    "must_include": _check_must_include,
    "required_data": _check_required_data,
    "citations": _check_citations,
    "banned_expressions": _check_banned_expressions,
    "no_placeholder": _check_no_placeholder,
    "min_length": _check_min_length,
}

# 由语义评估处理、不做程序化检查的验收项；保留在配置枚举里只为兼容旧规则
SEMANTIC_ONLY_CHECKS = {"terminology"}


def run_verification(path: str, skill: DocumentSkill, user_intent: str = "new",
                     known_sources: Optional[List[str]] = None,
                     has_knowledge: bool = False) -> Dict[str, Any]:
    """对已保存的 docx 执行程序化检查；术语实际用法交由语义评估。建议仅展示。"""
    file_path = Path(path)
    base = {"skill_id": skill.id, "file": str(file_path), "user_intent": user_intent,
            "recommendations": list(skill.recommendations)}

    if not file_path.exists():
        return {
            **base,
            "passed": False,
            "checks": [{"name": "file_exists", "passed": False, "detail": f"文件不存在：{file_path}"}],
            "skipped_checks": [],
            "summary": "0/1 项检查通过",
        }

    try:
        doc = Document(str(file_path))
    except Exception as exc:
        return {
            **base,
            "passed": False,
            "checks": [{"name": "file_readable", "passed": False, "detail": f"无法打开文档：{exc}"}],
            "skipped_checks": [],
            "summary": "0/1 项检查通过",
        }

    blocks = _collect_blocks(doc)
    checks: List[Dict[str, Any]] = []
    for name in skill.verification.checks:
        if name == "citation_sources":
            checks.append(_check_citation_sources(blocks, skill, known_sources, has_knowledge))
            continue
        if name == "unsupported_claims":
            checks.append(_check_unsupported_claims(blocks, skill))
            continue
        if name in SEMANTIC_ONLY_CHECKS:
            checks.append({
                "name": name,
                "passed": True,
                "skipped": True,
                "detail": "该项由语义评估处理，未做程序化检查",
            })
            continue
        func = CHECK_FUNCTIONS.get(name)
        if func is None:
            checks.append({"name": name, "passed": False,
                           "detail": f"未实现的验收项：{name}"})
            continue
        checks.append(func(blocks, skill))

    counted = [item for item in checks if not item.get("skipped")]
    passed_count = sum(1 for item in counted if item["passed"])
    return {
        **base,
        "passed": passed_count == len(counted),
        "checks": checks,
        "skipped_checks": [item["name"] for item in checks if item.get("skipped")],
        "summary": f"{passed_count}/{len(counted)} 项检查通过",
    }


def _build_citations(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把检索证据整理成可交付的引用清单（保留来源、标题、片段、查询与可用元数据）。"""
    citations: List[Dict[str, Any]] = []
    seen = set()
    for record in records:
        sources = record.get("sources") or ([record.get("source")] if record.get("source") else [])
        for source in sources:
            key = (str(source), str(record.get("query") or ""))
            if key in seen:
                continue
            seen.add(key)
            citations.append({
                "source": str(source),
                "source_type": record.get("source_type") or "",
                "title": record.get("title") or Path(str(source)).stem,
                "snippet": str(record.get("snippet") or "")[:200],
                "query": str(record.get("query") or ""),
                "node_ids": list(record.get("node_ids") or []),
                "file_mtime": record.get("file_mtime") or "",
            })
    return citations[:50]


def _semantic_rules_text(skill: DocumentSkill) -> str:
    """只挑出程序化检查无法可靠判断的规则。"""
    lines = []
    for i, section in enumerate(skill.structure.sections, 1):
        for j, requirement in enumerate(section.requirements, 1):
            lines.append(f"- S{i}.R{j} [{section.title}] {requirement}")
    if skill.task.professionalism:
        lines.append(f"- 专业程度与目标读者匹配：{skill.task.professionalism}")
    if skill.task.terminology:
        lines.append(f"- 涉及时统一术语写法：{'、'.join(skill.task.terminology)}；未涉及的术语不要求出现，不得据此判为缺项")
    if skill.evidence.cross_validation:
        lines.append("- 关键数据需要交叉验证，不同来源不一致时必须说明")
    if skill.evidence.conflict_policy:
        lines.append(f"- 来源冲突处理：{skill.evidence.conflict_policy}")
    if skill.research.time_range or skill.research.region:
        lines.append(
            f"- 数据口径应与时间/地域范围一致：{skill.research.time_range or '未限定时间'}，"
            f"{skill.research.region or '未限定地域'}"
        )
    if skill.research.credibility_rule:
        lines.append(f"- 来源可信度与时效性：{skill.research.credibility_rule}")
    if lines and (skill.research.data_fields or skill.research.questions):
        lines.append('- 原文已列示的数值/比率应称原文值或复算，不应一律标为推算；' +
                     ('缺失指标允许按已有输入推算，需公式与来源' if skill.evidence.derived_data == 'label' else '禁止补算原文未列示的指标'))
    return "\n".join(lines)


def render_source_evidence(records, limit=16000):
    seen = set()
    parts = []
    for record in records or []:
        for field in ('source_excerpt', 'content', 'snippet'):
            text = str(record.get(field) or '').strip()
            key = (str(record.get('source') or ''), text)
            if text and key not in seen:
                seen.add(key)
                parts.append(f"来源：{key[0]}\n资料片段：{text}")
            if text and field == 'content':
                break
    return '\n'.join(parts)[:limit]


def _validated_requirements(items, expected, blocks):
    accepted, errors = {}, []
    for item in items:
        section = expected.get(item.id)
        index = _match_heading(blocks, section.title) if section else None
        text = _section_text(blocks, index) if index is not None else ''
        quotes = item.quotes or ([item.quote] if item.quote else [])
        if not quotes or not all(q.strip() and _normalize(q) in _normalize(text) for q in quotes):
            errors.append({'id': item.id, 'reason': '缺少可定位的本章逐字引文'})
        else:
            accepted[item.id] = item
    return accepted, errors


def _issue_has_evidence(issue, doc_text, source_text):
    if not issue.quote or _normalize(issue.quote) not in doc_text:
        return False
    if issue.kind == 'consistency':
        return bool(issue.contradicting_quote and _normalize(issue.contradicting_quote) in doc_text)
    if issue.kind == 'source':
        return bool(issue.source_quote and _normalize(issue.source_quote) in source_text)
    return True


def _run_semantic_review(llm, skill: DocumentSkill, doc_path: str,
                         report: Dict[str, Any],
                         retrieval_report: Optional[Dict[str, Any]], records=None) -> Optional[Dict[str, Any]]:
    """对需要语义判断的规则做一次补充评估；失败不阻断，只标注未执行。"""
    if not skill.verification.semantic_review:
        return {"required": False, "executed": False, "passed": None, "summary": "配置已关闭语义评估", "issues": []}
    rules = _semantic_rules_text(skill)
    if not rules:
        return {"required": False, "executed": False, "passed": None, "summary": "没有需要语义判断的规则", "issues": []}
    if llm is None:
        return {"required": True, "executed": False, "passed": None, "summary": "未配置模型，跳过语义评估", "issues": []}

    try:
        doc = Document(doc_path)
    except Exception as exc:
        return {"required": True, "executed": False, "passed": None, "summary": f"无法读取文档：{exc}", "issues": []}

    blocks = _collect_blocks(doc)
    text = '\n'.join(f"[B{i}] {b['text']}" for i, b in enumerate(blocks, 1))
    truncated = len(text) > SEMANTIC_TEXT_LIMIT
    excerpt = text[:SEMANTIC_TEXT_LIMIT]

    existing_issues = [item for item in report.get("checks", []) if not item["passed"]]
    issue_lines = "\n".join(f"- {item['name']}：{item['detail']}" for item in existing_issues) or "（无）"
    missing_lines = "\n".join(
        f"- {q['id']}：{q['question']}"
        for q in (retrieval_report or {}).get("questions") or []
        if not q.get("covered")
    ) or "（无）"

    evidence_text = render_source_evidence(records, SEMANTIC_TEXT_LIMIT)
    prompt = (
        "你是文档质量审核助手。程序化检查已经覆盖章节齐全、字数、占位词、引用数量与引用来源是否存在，"
        "请只补充评估下列需要语义判断的规则：\n"
        f"{rules}\n\n"
        f"【文档正文{('（已截断）' if truncated else '')}】\n{excerpt}\n\n"
        f"【程序化检查已发现的问题】\n{issue_lines}\n"
        f"【检索未覆盖的问题】\n{missing_lines}\n\n"
        f"【可用资料片段（可能截断）】\n{evidence_text or '无资料片段，不得声称已核实事实'}\n"
        "核对正文与资料中的数值、单位、计算、缺失及冲突；资料不足时不得把无法核实说成已核实。\n"
        "每个issue必须提供quote，逐字复制有问题的正文，不得更改数字、单位或指标名；"
        "一致性问题kind=consistency并提供另一段contradicting_quote；来源问题kind=source并提供source_quote。"
        "不同指标和比较基期不可混淆；依据输入计算的结果须核对公式、单位及来源，不将推算直接等同于编造。"
        "‘无法判断’‘不作推测’是边界说明，建议未来核查也不是已发生事实。\n"
        "对每个Sx.Ry要求返回一项requirements：id、satisfied、该章节内逐字quote或多个独立quotes与detail；不得遗漏任何要求。"
        "禁止用省略号把两段原文拼成一个quote；多处证据分别放入quotes数组。引文定位不等于推断正确。"
        "不能以语义意见覆盖程序检查结果，不能把条件性解释当作已证实的成因。"
        "只报告有原文依据的违规，没有问题则passed=true。"
    )
    result = llm_model_invoke(llm, prompt, SemanticReview, retry=1)
    if result is None:
        return {"required": True, "executed": False, "passed": None, "summary": "语义评估调用失败", "issues": []}
    expected = {f'S{i}.R{j}': section for i, section in enumerate(skill.structure.sections, 1)
                for j, _ in enumerate(section.requirements, 1)}
    assessed, errors = _validated_requirements(result.requirements, expected, blocks)
    attempts = [{'stage': 'review', 'raw_review': result.model_dump(), 'assessment_errors': errors}]
    missing = set(expected) - set(assessed)
    if missing:
        repair_prompt = prompt + '\n只补充以下要求的逐字引文；返回requirements，不重新评判问题或修改正文：' + ', '.join(sorted(missing))
        repair = llm_model_invoke(llm, repair_prompt, RequirementReview, retry=1)
        if repair is None:
            attempts.append({'stage': 'repair_requirements', 'error': '引文修复调用失败'})
        else:
            fixed, errors = _validated_requirements(repair.requirements, {k: expected[k] for k in missing}, blocks)
            assessed.update(fixed)
            attempts.append({'stage': 'repair_requirements', 'raw_review': repair.model_dump(), 'assessment_errors': errors})

    # 问题只评判一次；引文补充不增删原来的指控，避免多轮状态互相覆盖。
    issues = list(result.issues)
    for item in assessed.values():
        if not item.satisfied:
            issues.append(SemanticIssue(rule=item.id, detail=item.detail or '章节语义要求未满足',
                                       quote=(item.quotes or [item.quote])[0], kind='missing_requirement'))
    valid, unverified = [], []
    for issue in issues:
        grounded = _issue_has_evidence(issue, _normalize(excerpt), _normalize(evidence_text))
        record = {**issue.model_dump(), 'evidence_status': 'quote_located_not_fact_verified' if grounded else 'invalid_quote'}
        (valid if grounded else unverified).append(record)
    if not result.passed and not issues:
        unverified.append({'rule': 'semantic_result', 'detail': '模型判未通过但没有提供可定位的指控'})
    unchecked = sorted(set(expected) - set(assessed))
    complete = not truncated and not unverified and not unchecked
    return {
        "executed": True,
        "required": True,
        "complete": complete,
        "passed": False if valid else True if complete else None,
        "summary": f"已定位引文的问题{len(valid)}项（结论仍需核实），无效引文指控{len(unverified)}项，未完成语义要求{len(unchecked)}项" +
                   (f"；正文仅覆盖前{SEMANTIC_TEXT_LIMIT}字符" if truncated else ''),
        "raw_model_summary": result.summary,
        "issues": valid,
        "unverified_issues": unverified,
        "unchecked_requirements": unchecked,
        "requirements": [item.model_dump() for item in assessed.values()],
        "review_attempts": attempts,
    }


def _finalize_report(report: Dict[str, Any], citations: List[Dict[str, Any]],
                     semantic: Optional[Dict[str, Any]],
                     retrieval_report: Optional[Dict[str, Any]], research_required=False) -> Dict[str, Any]:
    """合并程序化检查、语义评估与检索缺失，生成最终未满足项清单。"""
    report["programmatic_passed"] = bool(report.get("passed", False))
    report["citations"] = citations[:20]
    report["semantic_review"] = semantic or {
        "executed": False, "passed": None, "summary": "未执行语义评估", "issues": []
    }
    report["retrieval_complete"] = (
        not retrieval_report.get("missing") and all(q.get("covered") for q in retrieval_report.get("questions", []))
        if retrieval_report is not None else None
    )
    data_check = next((item for item in report.get("checks", []) if item["name"] == "required_data"), None)
    report["data_complete"] = data_check["passed"] if data_check else None

    unmet: List[str] = []
    for item in report.get("checks") or []:
        if not item["passed"]:
            unmet.append(f"{item['name']}：{item['detail']}")
    for question in (retrieval_report or {}).get("questions") or []:
        if not question.get("covered"):
            unmet.append(f"检索缺失（{question['id']}）：{question['question']}")
    listed_ids = {q["id"] for q in (retrieval_report or {}).get("questions", [])}
    unmet.extend(f"检索缺失（{qid}）" for qid in (retrieval_report or {}).get("missing", []) if qid not in listed_ids)
    retrieval_unknown = research_required and report["retrieval_complete"] is None
    if retrieval_unknown:
        unmet.append("检索覆盖未完成：没有检索覆盖报告")
    for issue in (semantic or {}).get("issues") or []:
        unmet.append(f"语义检查：{issue.get('rule')}｜{issue.get('detail')}")
    semantic_incomplete = bool(semantic and semantic.get("required") and (
        not semantic.get("executed") or semantic.get("complete") is False
    ))
    if semantic_incomplete:
        unmet.append("语义检查未完成：" + semantic.get("summary", ""))
    semantic_failed = bool(semantic and semantic.get("executed") and semantic.get("passed") is False)
    failed = not report["programmatic_passed"] or semantic_failed
    incomplete = report["retrieval_complete"] is False or semantic_incomplete or retrieval_unknown
    report["passed"] = not failed and not incomplete
    report["status"] = "failed" if failed else "incomplete" if incomplete else "passed"
    report["summary"] += "；综合验收：" + {"failed": "未通过", "incomplete": "未完成", "passed": "通过"}[report["status"]]
    report["unmet_requirements"] = unmet
    return report


def create_verify_node(llm=None):
    def verify_node(state: AgentState) -> AgentState:
        # 生成阶段已失败时不做验收，避免对不存在或未写完的文件产生误导性报告
        if state.get("state") == "error":
            return {**state, "verification_report": None}
        skill = coerce_skill(state.get("skill"))
        if skill is None:
            return {**state, "verification_report": None}

        save_path = str(state.get("save_path") or "")
        if not save_path:
            print("【验收】没有可验收的输出文档，跳过")
            return {**state, "verification_report": None}

        records = list(state.get("evidence_records") or [])
        known_sources: List[str] = []
        has_knowledge = False
        for record in records:
            known_sources.extend(str(item) for item in (record.get("sources") or []))
            if record.get("source"):
                known_sources.append(str(record["source"]))
            if record.get("source_type") == "knowledge":
                has_knowledge = True

        report = run_verification(
            save_path,
            skill,
            user_intent=str(state.get("user_intent") or "new"),
            known_sources=known_sources,
            has_knowledge=has_knowledge,
        )
        citations = _build_citations(records)
        semantic = _run_semantic_review(
            llm, skill, save_path, report, state.get("retrieval_report"), records
        )
        report = _finalize_report(report, citations, semantic, state.get("retrieval_report"),
                                  research_required=bool(skill.research.questions or skill.research.data_fields))

        print(f"【验收】{skill.name}｜{report['summary']}｜文件：{report['file']}")
        for item in report["checks"]:
            mark = "跳过" if item.get("skipped") else ("通过" if item["passed"] else "未通过")
            print(f"    - {item['name']}：{mark}｜{item['detail']}")
        review = report["semantic_review"]
        if review.get("executed"):
            mark = '通过' if review.get('passed') is True else '未通过' if review.get('passed') is False else '待核实'
            print(f"    - semantic_review：{mark}｜"
                  f"{review.get('summary') or '（无说明）'}")
        elif skill.verification.semantic_review:
            print(f"    - semantic_review：未执行｜{review.get('summary')}")
        print(f"【引用】整理出 {len(citations)} 条可追溯来源")
        if report["unmet_requirements"]:
            print("【验收】仍未满足的要求：")
            for item in report["unmet_requirements"]:
                print(f"    - {item}")
        if not report["passed"]:
            print("【验收】存在未通过项（按配置只报告，不阻断流程，文档已保存）")
        return {
            **state,
            "verification_report": report,
            "citations": citations,
            "unmet_requirements": report["unmet_requirements"],
        }

    return verify_node
