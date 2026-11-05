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

PLACEHOLDER_PATTERN = re.compile(
    r"(待补充|待完善|待填写|待确认|此处省略|后续补充|TODO|TBD|XXX)", re.IGNORECASE
)
# 含这些措辞的段落属于「判断/预测」，必须带引用或显式标注无依据
CLAIM_MARKER_PATTERN = re.compile(r"(预计|预测|有望|推测|判断|将会|未来|可能)")
UNSUPPORTED_MARK = "未检索到依据"
SEMANTIC_TEXT_LIMIT = 12000


class SemanticIssue(BaseModel):
    rule: str = Field(description="违反的规则名称")
    detail: str = Field(description="具体问题与位置/原文片段")
    severity: Literal["high", "medium", "low"] = Field(default="medium", description="严重程度")


class SemanticReview(BaseModel):
    passed: bool = Field(description="是否未发现语义规则违规")
    issues: List[SemanticIssue] = Field(default_factory=list, description="发现的问题列表")
    summary: str = Field(default="", description="总体评价")


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
        for row in block.rows:
            for cell in row.cells:
                parts.append(cell.text or "")
        return "\n".join(parts)
    return ""


def _heading_level(block: Any) -> Optional[int]:
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
            "level": _heading_level(block),
        })
    return blocks


def _match_heading(blocks: List[Dict[str, Any]], title: str) -> Optional[int]:
    target = _normalize(title)
    if not target:
        return None
    for index, item in enumerate(blocks):
        if not item["level"]:
            continue
        text = item["normalized"]
        if target in text or text in target:
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


def _check_required_sections(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    missing = []
    for section in skill.structure.sections:
        if not section.required:
            continue
        if _match_heading(blocks, section.title) is None:
            missing.append(section.title)
    return {
        "name": "required_sections",
        "passed": not missing,
        "detail": "必填章节齐全" if not missing else "缺少必填章节：" + "、".join(missing),
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


def _check_required_data(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    fields = skill.research.data_fields
    if not fields:
        return {"name": "required_data", "passed": True, "detail": "未配置必须收集的数据字段"}
    text = _normalize(_all_text(blocks))
    missing = [field for field in fields if _normalize(field) not in text]
    return {
        "name": "required_data",
        "passed": not missing,
        "detail": "必需数据字段均已出现" if not missing else "正文缺少数据字段：" + "、".join(missing),
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
        if not CLAIM_MARKER_PATTERN.search(text):
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
    hits = PLACEHOLDER_PATTERN.findall(_all_text(blocks))
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


def _check_terminology(blocks, skill: DocumentSkill) -> Dict[str, Any]:
    terms = []
    for term in skill.task.terminology:
        # 术语可能带括号说明，只校验主术语是否出现
        main = re.split(r"[（(]", term, maxsplit=1)[0].strip()
        if main:
            terms.append(main)
    if not terms:
        return {"name": "terminology", "passed": True, "detail": "未配置统一术语"}
    text = _normalize(_all_text(blocks))
    missing = [term for term in terms if _normalize(term) not in text]
    return {
        "name": "terminology",
        "passed": not missing,
        "detail": "统一术语均已出现" if not missing else "正文未使用术语：" + "、".join(missing),
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
    "terminology": _check_terminology,
    "banned_expressions": _check_banned_expressions,
    "no_placeholder": _check_no_placeholder,
    "min_length": _check_min_length,
}


def run_verification(path: str, skill: DocumentSkill, user_intent: str = "new",
                     known_sources: Optional[List[str]] = None,
                     has_knowledge: bool = False) -> Dict[str, Any]:
    """对已保存的 docx 执行 Skill 要求的检查；返回可序列化的报告。"""
    file_path = Path(path)
    base = {"skill_id": skill.id, "file": str(file_path), "user_intent": user_intent}

    if not file_path.exists():
        return {
            **base,
            "passed": False,
            "checks": [{"name": "file_exists", "passed": False, "detail": f"文件不存在：{file_path}"}],
            "summary": "0/1 项检查通过",
        }

    try:
        doc = Document(str(file_path))
    except Exception as exc:
        return {
            **base,
            "passed": False,
            "checks": [{"name": "file_readable", "passed": False, "detail": f"无法打开文档：{exc}"}],
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
        func = CHECK_FUNCTIONS.get(name)
        if func is None:
            continue
        checks.append(func(blocks, skill))

    passed_count = sum(1 for item in checks if item["passed"])
    return {
        **base,
        "passed": passed_count == len(checks),
        "checks": checks,
        "summary": f"{passed_count}/{len(checks)} 项检查通过",
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
    if skill.task.professionalism:
        lines.append(f"- 专业程度与目标读者匹配：{skill.task.professionalism}")
    if skill.task.terminology:
        lines.append(f"- 术语口径应统一为：{'、'.join(skill.task.terminology)}")
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
    return "\n".join(lines)


def _run_semantic_review(llm, skill: DocumentSkill, doc_path: str,
                         report: Dict[str, Any],
                         retrieval_report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """对需要语义判断的规则做一次补充评估；失败不阻断，只标注未执行。"""
    if not skill.verification.semantic_review:
        return {"executed": False, "passed": None, "summary": "配置已关闭语义评估", "issues": []}
    rules = _semantic_rules_text(skill)
    if not rules:
        return {"executed": False, "passed": None, "summary": "没有需要语义判断的规则", "issues": []}
    if llm is None:
        return {"executed": False, "passed": None, "summary": "未配置模型，跳过语义评估", "issues": []}

    try:
        doc = Document(doc_path)
    except Exception as exc:
        return {"executed": False, "passed": None, "summary": f"无法读取文档：{exc}", "issues": []}

    blocks = _collect_blocks(doc)
    text = _all_text(blocks)
    truncated = len(text) > SEMANTIC_TEXT_LIMIT
    excerpt = text[:SEMANTIC_TEXT_LIMIT]

    existing_issues = [item for item in report.get("checks", []) if not item["passed"]]
    issue_lines = "\n".join(f"- {item['name']}：{item['detail']}" for item in existing_issues) or "（无）"
    missing_lines = "\n".join(
        f"- {q['id']}：{q['question']}"
        for q in (retrieval_report or {}).get("questions") or []
        if not q.get("covered")
    ) or "（无）"

    prompt = (
        "你是文档质量审核助手。程序化检查已经覆盖章节齐全、字数、占位词、引用数量与引用来源是否存在，"
        "请只补充评估下列需要语义判断的规则：\n"
        f"{rules}\n\n"
        f"【文档正文{('（已截断）' if truncated else '')}】\n{excerpt}\n\n"
        f"【程序化检查已发现的问题】\n{issue_lines}\n"
        f"【检索未覆盖的问题】\n{missing_lines}\n\n"
        "只报告确实违反上述规则的问题，并给出原文片段作为依据；没有问题则 passed=true。"
    )
    result = llm_model_invoke(llm, prompt, SemanticReview)
    if result is None:
        return {"executed": False, "passed": None, "summary": "语义评估调用失败", "issues": []}
    return {
        "executed": True,
        "passed": bool(result.passed),
        "summary": result.summary,
        "issues": [issue.model_dump() for issue in result.issues],
    }


def _finalize_report(report: Dict[str, Any], citations: List[Dict[str, Any]],
                     semantic: Optional[Dict[str, Any]],
                     retrieval_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """合并程序化检查、语义评估与检索缺失，生成最终未满足项清单。"""
    report["programmatic_passed"] = bool(report.get("passed", False))
    report["citations"] = citations[:20]
    report["semantic_review"] = semantic or {
        "executed": False, "passed": None, "summary": "未执行语义评估", "issues": []
    }
    if semantic and semantic.get("executed") and not semantic.get("passed"):
        report["passed"] = False

    unmet: List[str] = []
    for item in report.get("checks") or []:
        if not item["passed"]:
            unmet.append(f"{item['name']}：{item['detail']}")
    for question in (retrieval_report or {}).get("questions") or []:
        if not question.get("covered"):
            unmet.append(f"检索缺失（{question['id']}）：{question['question']}")
    for issue in (semantic or {}).get("issues") or []:
        unmet.append(f"语义检查：{issue.get('rule')}｜{issue.get('detail')}")
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
        known_sources.extend(str(path) for path in (state.get("input_file_path") or []))

        report = run_verification(
            save_path,
            skill,
            user_intent=str(state.get("user_intent") or "new"),
            known_sources=known_sources,
            has_knowledge=has_knowledge,
        )
        citations = _build_citations(records)
        semantic = _run_semantic_review(
            llm, skill, save_path, report, state.get("retrieval_report")
        )
        report = _finalize_report(report, citations, semantic, state.get("retrieval_report"))

        print(f"【验收】{skill.name}｜{report['summary']}｜文件：{report['file']}")
        for item in report["checks"]:
            mark = "通过" if item["passed"] else "未通过"
            print(f"    - {item['name']}：{mark}｜{item['detail']}")
        review = report["semantic_review"]
        if review.get("executed"):
            print(f"    - semantic_review：{'通过' if review.get('passed') else '未通过'}｜"
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
