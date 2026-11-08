from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from llm_client import llm_invoke, llm_model_invoke, extract_json_from_text
from agent_core import AgentState
from retrieved_context import build_retrieved_info, enforce_full_document_names
from document_agent.write_tool.word_tool import replace_node_with_data, DocxRoot, resolve_save_path, save_document
from document_agent.skills.render import render_full_brief, render_retrieval_gaps
from document_agent.skills.schema import coerce_skill
from document_agent.skills.content_checks import normalize_heading, strip_heading_number
from document_agent.write_tool.document_layout import format_report_tables
from document_agent.agent.verify_node import document_length, document_text, section_issues, render_source_evidence, heading_level


def _normalize_llm_sections(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sections = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            sections.append({"title": item.strip(), "level": 1})
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("section") or "").strip()
            if title:
                sections.append({**item, "title": title})
    return sections


def _plan_from_skill(skill) -> List[Dict[str, Any]]:
    return [section.model_dump() for section in skill.structure.sections]


def _section_requirement(section: Dict[str, Any]) -> str:
    return (
        f"标题：{section['title']}；大纲级别：{section.get('level', 1)}\n"
        f"必须包含：{'、'.join(section.get('must_include') or [])}\n"
        f"语义要求：{'；'.join(section.get('requirements') or [])}\n"
        f"本章必需数据：{'、'.join(section.get('data_fields') or [])}\n"
        f"正文最少字符：{section.get('min_body_chars', 1)}\n"
        f"需要证据类型：{'、'.join(section.get('evidence_types') or [])}\n"
        f"写作要求：{section.get('guidance') or ''}\n"
        f"必须引用：{section.get('evidence_required', False)}；最少引用：{section.get('min_citations', 0)}"
    )


def _base_document(skill):
    template = str(skill.template_path or "") if skill else ""
    if template and Path(template).is_file():
        return Document(template)
    return Document()


def _apply_base_format(doc, skill):
    style = doc.styles['Normal']
    # 模板已有样式时保留；配置仅覆盖明确声明的属性。
    if skill is None or not skill.template_path:
        style.font.name = 'Times New Roman'
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), '宋体')
    if skill is None:
        return
    fmt = skill.structure.format
    if fmt.body_font:
        style.font.name = fmt.body_font
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), fmt.body_font)
    if fmt.body_size is not None:
        style.font.size = Pt(fmt.body_size)
    if fmt.line_spacing is not None:
        style.paragraph_format.line_spacing = fmt.line_spacing
    if fmt.first_line_indent is not None:
        style.paragraph_format.first_line_indent = Pt(fmt.first_line_indent)


def _numbered_title(title, index, numbering):
    title = strip_heading_number(title) if numbering else title
    digits = '一二三四五六七八九'
    def chinese(n):
        if n < 10:
            return digits[n - 1]
        tens, ones = divmod(n, 10)
        return (digits[tens - 1] if tens > 1 else '') + '十' + (digits[ones - 1] if ones else '')
    if numbering == '一、':
        return chinese(index) + '、' + title
    if numbering == '（一）':
        return '（' + chinese(index) + '）' + title
    if numbering in ('1.', '1、'):
        return str(index) + numbering[-1] + title
    return title


def _render_draft(result, section, index, skill):
    draft = Document()
    _apply_base_format(draft, skill)
    for item in result.items:
        if item.type == 'paragraph':
            replace_node_with_data(draft.add_paragraph(), item.Paragraph)
        elif item.type == 'table':
            replace_node_with_data(draft.add_table(rows=0, cols=0), item.Table)
    roots = [p for p in draft.paragraphs if normalize_heading(p.text) == normalize_heading(section['title'])]
    if len(roots) > 1:
        raise ValueError('重复生成当前章标题')
    for p in draft.paragraphs:
        level = heading_level(p)
        if level and level <= section.get('level', 1) and all(p._element is not r._element for r in roots):
            raise ValueError('当前章中出现其他同级章节：' + p.text)
    heading = roots[0] if roots else draft.add_heading('', level=section.get('level', 1))
    if not roots:
        heading._element.getparent().remove(heading._element)
        draft._element.body.insert(0, heading._element)
    heading.style = draft.styles[f"Heading {section.get('level', 1)}"]
    heading.text = _numbered_title(section['title'], index, skill.structure.numbering if skill else '')
    return draft


def _format_headings(doc, skill):
    if skill is None:
        return
    fmt = skill.structure.format
    for p in doc.paragraphs:
        level = heading_level(p)
        if not level:
            continue
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.first_line_indent = Pt(0)
        for run in p.runs:
            if not skill.template_path:
                run.font.color.rgb = RGBColor(0, 0, 0)
            if fmt.heading_font:
                run.font.name = fmt.heading_font
                run._element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), fmt.heading_font)
            if level in fmt.heading_sizes:
                run.font.size = Pt(fmt.heading_sizes[level])
            if fmt.heading_bold is not None:
                run.bold = fmt.heading_bold


def _chapter_budget(doc, sections, index, skill):
    """总字符上限包含标题、引用、表格；为后续每章保留最低空间。"""
    maximum = skill.task.length_target.max_chars if skill else None
    if maximum is None:
        return None
    remaining = maximum - document_length(doc)
    reserved = sum(s.get('min_body_chars', 1) + len(s['title']) for s in sections[index:])
    budget = remaining - reserved
    if budget < sections[index - 1].get('min_body_chars', 1) + len(sections[index - 1]['title']):
        raise ValueError('全文字符预算不足以满足后续章节的最低正文要求')
    return budget


def create_new_docx_node(llm, knowledge_mode: str = "local"):
    """按 Skill 或模型规划逐章生成，保留远程引用全名补全与保存逻辑。"""
    def docx_node(state: AgentState) -> AgentState:
        messages = state["messages"]
        retrieved_info = build_retrieved_info(state.get("retrieved_content") or [], mode=knowledge_mode)
        skill = coerce_skill(state.get("skill"))
        skill_blocks = "\n\n".join(block for block in (
            render_full_brief(skill), render_retrieval_gaps(state.get("retrieval_report"))
        ) if block)
        if skill is not None and skill.structure.sections:
            sections = _plan_from_skill(skill)
            print(f"【Skill】按「{skill.name}」的章节清单生成")
        else:
            prompt = (
                "你是文档写作助手，请按用户要求规划章节，输出 JSON 列表，元素为章节标题字符串。\n"
                f"用户输入：{messages}\n{retrieved_info}\n{skill_blocks}"
            )
            response = llm_invoke(llm, prompt)
            content = getattr(response, "content", None)
            matches = extract_json_from_text(content, return_all=True) if content else []
            try:
                sections = _normalize_llm_sections(json.loads(matches[0])) if matches else []
            except (ValueError, TypeError, IndexError):
                sections = []
            if not sections:
                return {**state, "state": "error", "error": "规划文档章节时，模型未返回有效章节列表"}

        outline = "\n".join(section["title"] for section in sections)
        doc = _base_document(skill)
        _apply_base_format(doc, skill)
        source_evidence = render_source_evidence(state.get('evidence_records') or [])
        generation_report = []
        for index, section in enumerate(sections, 1):
            print(f"编写{section['title']}章节")
            try:
                budget = _chapter_budget(doc, sections, index, skill)
            except ValueError as exc:
                return {**state, 'state': 'error', 'error': str(exc), 'generation_report': generation_report}
            prompt = (
                "你是一个文档写作助手，请根据用户需求、当前章节和来源证据生成段落与表格。\n"
                "仅生成当前章节，不要重复生成其他章节；标题必须原样出现。\n"
                "标题段落设置 heading_level 为指定大纲级别，正文不设置。正文默认不加粗，标题加粗。\n"
                "优先遵循用户及 Skill 格式要求；未指定时按文档用途设计一致的字体字号。\n"
                f"用户输入：{messages}\n整个文档的大纲：\n{outline}\n"
                f"当前要编写的章节：\n{_section_requirement(section)}\n"
                f"本章最多字符数（含标题、引用、表格，去空白）：{budget if budget is not None else '未设上限'}；为后续章节合理分配篇幅。\n"
                f"{retrieved_info}\n{skill_blocks}"
                f"\n【固定来源资料】\n{source_evidence}\n"
                f"【已定稿章节，仅作一致性参照，不要重复生成】\n{document_text(doc)[-10000:]}\n"
                "已提供的字段不可称为缺失；派生值明确列出计算式和输入来源，跨章口径一致。"
                "原文已列示的数值和比率只能称为原文值/复算，只有原文未列示而依据输入计算的值才称推算。"
                "数值、单位和推导须核对来源，不得超出证据作断言；避免重复内容。总标题遵循用户需求或模板。"
                "本章必须有正文，不能只生成标题；篇幅按整个文档的章节数分配，不要把全文字数都用在一章。"
            )
            limit = skill.verification.max_section_attempts if skill else 3
            failures = []
            draft = None
            for attempt in range(1, limit + 1):
                feedback = '\n【上次检查未通过，请重新输出完整当前章】\n' + '\n'.join(failures[-1]) if failures else ''
                result = llm_model_invoke(llm, prompt + feedback, DocxRoot, retry=1)
                try:
                    if result is None:
                        raise ValueError('模型未返回可解析章节')
                    candidate = _render_draft(result, section, index, skill)
                    issues = section_issues(candidate, skill, section)
                    chars = document_length(candidate)
                    if budget is not None and chars > budget:
                        issues.append(f'本章{chars}字符，超过分配上限{budget}；删除重复声明和数据复述，保留必需指标、证据与结论')
                except (ValueError, TypeError, AttributeError) as exc:
                    issues = [str(exc)]
                if not issues:
                    draft = candidate
                    break
                failures.append(issues)
                print(f"【章节检查】{section['title']} 第{attempt}/{limit}次未通过：{'；'.join(issues)}")
            generation_report.append({'section': section['title'], 'attempts': attempt, 'failures': failures,
                                      'passed': draft is not None, 'max_chars': budget,
                                      'actual_chars': document_length(draft) if draft is not None else None})
            if draft is None:
                return {**state, 'state': 'error', 'error': f"章节「{section['title']}」在{limit}次内仍未通过检查，未保存文档：{'；'.join(failures[-1])}",
                        'generation_report': generation_report}
            for element in draft._element.body:
                if element.tag != qn('w:sectPr'):
                    doc._element.body.insert(len(doc._element.body) - 1, deepcopy(element))

        if str(knowledge_mode).strip().lower() == "remote":
            for old, new, count in enforce_full_document_names(doc, state.get("document_citations") or []):
                print(f"【引用规范】把简称引用 {old} 补全为 {new}（{count} 处）")
        _format_headings(doc, skill)
        format_report_tables(doc, skill)
        if skill and skill.task.length_target.max_chars is not None and document_length(doc) > skill.task.length_target.max_chars:
            return {**state, 'state': 'error', 'error': '最终全文字符数超过上限，未保存文档', 'generation_report': generation_report}
        save_path = save_document(doc, resolve_save_path(state.get("save_path")))
        print("保存到：", str(save_path))
        return {**state, "state": "succeeded", "save_path": str(save_path), 'generation_report': generation_report}
    return docx_node
