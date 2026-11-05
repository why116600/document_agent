from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from llm_client import llm_invoke, llm_model_invoke, extract_json_from_text
from agent_core import AgentState
from retrieved_context import build_retrieved_info, enforce_full_document_names
from document_agent.write_tool.word_tool import replace_node_with_data, DocxRoot, resolve_save_path, save_document
from document_agent.skills.render import render_full_brief, render_retrieval_gaps
from document_agent.skills.schema import coerce_skill


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
        for section in sections:
            print(f"编写{section['title']}章节")
            prompt = (
                "你是一个文档写作助手，请根据用户需求、当前章节和来源证据生成段落与表格。\n"
                "仅生成当前章节，不要重复生成其他章节；标题必须原样出现。\n"
                "标题段落设置 heading_level 为指定大纲级别，正文不设置。正文默认不加粗，标题加粗。\n"
                "优先遵循用户及 Skill 格式要求；未指定时按文档用途设计一致的字体字号。\n"
                f"用户输入：{messages}\n整个文档的大纲：\n{outline}\n"
                f"当前要编写的章节：\n{_section_requirement(section)}\n"
                f"{retrieved_info}\n{skill_blocks}"
            )
            result = llm_model_invoke(llm, prompt, DocxRoot)
            if result is None:
                return {**state, "state": "error", "error": "写作失败"}
            for item in result.items:
                if item.type == "paragraph":
                    replace_node_with_data(doc.add_paragraph(""), item.Paragraph)
                elif item.type == "table":
                    replace_node_with_data(doc.add_table(rows=0, cols=0), item.Table)

        if str(knowledge_mode).strip().lower() == "remote":
            for old, new, count in enforce_full_document_names(doc, state.get("document_citations") or []):
                print(f"【引用规范】把简称引用 {old} 补全为 {new}（{count} 处）")
        save_path = save_document(doc, resolve_save_path(state.get("save_path")))
        print("保存到：", str(save_path))
        return {**state, "state": "succeeded", "save_path": str(save_path)}
    return docx_node
