"""把 Skill 配置渲染成提示词片段。

所有函数都接受 ``DocumentSkill`` 或状态里的 Skill 字典；没有 Skill 时返回空字符串，
保证未启用 Skill 的调用路径与改造前完全一致。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from .schema import DocumentSkill, coerce_skill

SOURCE_LABELS = {
    "file": "file（用户提供的参考文件）",
    "knowledge": "knowledge（组织知识库）",
}

ON_EMPTY_LABELS = {
    "rewrite_query": "改写检索 query 后继续检索",
    "fallback_source": "切换到下一个可用来源继续检索",
    "mark_missing": "停止检索并在文档中显式标注信息缺失",
}


def _skill(value: Any) -> Optional[DocumentSkill]:
    return coerce_skill(value)


def _join(lines: List[str]) -> str:
    return "\n".join(line for line in lines if line)


def render_task_brief(value: Any) -> str:
    """文档用途、读者、风格、篇幅、术语等总体写作要求。"""
    skill = _skill(value)
    if skill is None:
        return ""
    task = skill.task
    lines = ["【文档写作规范】"]
    if task.purpose:
        lines.append(f"- 文档用途：{task.purpose}")
    if task.audience:
        lines.append(f"- 目标读者：{task.audience}")
    if task.tone:
        lines.append(f"- 写作风格：{task.tone}")
    if task.professionalism:
        lines.append(f"- 专业程度：{task.professionalism}")
    if task.language:
        lines.append(f"- 输出语言：{task.language}")
    if task.length_target.min_chars or task.length_target.max_chars:
        parts = []
        if task.length_target.min_chars:
            parts.append(f"不少于 {task.length_target.min_chars} 字")
        if task.length_target.max_chars:
            parts.append(f"不超过 {task.length_target.max_chars} 字")
        lines.append(f"- 篇幅要求：{'，'.join(parts)}")
    if task.terminology:
        lines.append(f"- 统一术语写法：{'、'.join(task.terminology)}")
    if task.banned_expressions:
        lines.append(f"- 禁用表达：{'、'.join(task.banned_expressions)}")
    for item in task.extra_requirements:
        lines.append(f"- 其它要求：{item}")
    return _join(lines)


def render_structure_brief(value: Any) -> str:
    """章节清单、顺序、编号体例与输出格式。"""
    skill = _skill(value)
    if skill is None or not skill.structure.sections:
        return ""
    structure = skill.structure
    order_text = "必须严格按下列顺序输出" if structure.order == "strict" else "下列章节必须包含，顺序可调整"
    lines = [f"【章节结构】{order_text}，章节标题必须原样使用，不得改写成同义标题："]
    for index, section in enumerate(structure.sections, 1):
        marks = []
        if section.required:
            marks.append("必填")
        if section.evidence_required or section.min_citations > 0:
            marks.append("需引用")
        suffix = f"（{'、'.join(marks)}）" if marks else ""
        lines.append(f"{index}. {section.title}（大纲级别 {section.level}）{suffix}")
        if section.must_include:
            lines.append(f"   必须包含要点：{'、'.join(section.must_include)}")
        if section.requirements:
            lines.append(f"   语义要求：{'；'.join(section.requirements)}")
        if section.data_fields:
            lines.append(f"   本章数据字段：{'、'.join(section.data_fields)}")
        lines.append(f"   正文不少于{section.min_body_chars}字符，不得只输出标题")
        if section.evidence_types:
            lines.append(f"   需要证据类型：{'、'.join(section.evidence_types)}")
        if section.guidance:
            lines.append(f"   写作要求：{section.guidance}")
    if structure.numbering:
        lines.append(f"- 章节编号体例：{structure.numbering}")
    if structure.output_format:
        lines.append(f"- 输出格式：{structure.output_format}")
    return _join(lines)


def render_format_brief(value: Any) -> str:
    """字体字号与段落格式。"""
    skill = _skill(value)
    if skill is None:
        return ""
    fmt = skill.structure.format
    lines: List[str] = []
    if fmt.body_font:
        lines.append(f"正文中文字体：{fmt.body_font}")
    if fmt.body_size:
        lines.append(f"正文字号：{fmt.body_size} 磅")
    if fmt.heading_font:
        lines.append(f"标题字体：{fmt.heading_font}")
    if fmt.heading_sizes:
        sizes = "、".join(f"{level} 级 {size} 磅" for level, size in sorted(fmt.heading_sizes.items()))
        lines.append(f"标题字号：{sizes}")
    if fmt.heading_bold is not None:
        lines.append("标题加粗" if fmt.heading_bold else "标题不加粗")
    if fmt.line_spacing:
        lines.append(f"行距：{fmt.line_spacing} 倍")
    if fmt.first_line_indent:
        lines.append(f"首行缩进：{fmt.first_line_indent} 磅")
    if not lines:
        return ""
    return "【格式要求】\n" + "\n".join(f"- {line}" for line in lines)


def render_research_brief(value: Any) -> str:
    """检索目标、来源优先级、真实可用的检索参数与迭代规则。"""
    skill = _skill(value)
    if skill is None:
        return ""
    research = skill.research
    lines = ["【检索规范】"]
    if research.questions:
        lines.append("- 必须回答的研究问题：")
        for index, question in enumerate(research.questions, 1):
            lines.append(f"  {index}. {question}")
    if research.data_fields:
        lines.append(f"- 必须收集的数据字段：{'、'.join(research.data_fields)}")
    if research.field_aliases:
        lines.append(f"- 字段等价写法：{research.field_aliases}")
    if research.time_range:
        lines.append(f"- 检索时间范围：{research.time_range}（仅作为筛选依据，来源若无时间信息需注明）")
    if research.region:
        lines.append(f"- 地域范围：{research.region}")
    if research.source.priority:
        labels = " → ".join(SOURCE_LABELS.get(item, item) for item in research.source.priority)
        lines.append(f"- 来源优先级（仅可使用这些来源）：{labels}")
    knowledge = research.knowledge
    lines.append(
        f"- 知识库检索参数：召回 {knowledge.top_k} 条，最低相似度 {knowledge.min_similarity}，"
        f"邻居展开{'开启' if knowledge.expand_neighbors else '关闭'}（最多 {knowledge.max_expansions} 轮）"
    )
    if research.file.max_chars:
        lines.append(f"- 单个文件送入模型的最大字符数：{research.file.max_chars}")
    lines.append(f"- 检索轮次上限：{research.max_rounds} 次工具调用")
    lines.append(f"- 单个研究问题最多尝试 {research.per_question_attempts} 次（跨来源/文件）")
    if research.query_expansion:
        lines.append("- 结果不足时允许改写检索 query 继续检索")
    if research.on_empty:
        lines.append(f"- 检索为空时的处理：{ON_EMPTY_LABELS.get(research.on_empty, research.on_empty)}")
    if research.stop_when:
        lines.append(f"- 提前结束条件：{research.stop_when}")
    if research.stop_after_no_new_evidence:
        lines.append(f"- 程序停止条件：连续 {research.stop_after_no_new_evidence} 轮没有新增证据")
    if research.credibility_rule:
        lines.append(f"- 来源可信度与时效性要求：{research.credibility_rule}")
    return _join(lines)


def render_evidence_brief(value: Any) -> str:
    """引用规范、证据强度与冲突处理。"""
    skill = _skill(value)
    if skill is None:
        return ""
    evidence = skill.evidence
    lines = ["【证据与引用规范】"]
    lines.append('- 派生指标：' + ('允许依据已提供数据计算，必须标明计算式、输入来源和“推算”；所有章节使用同一口径，不得一处称未推算而另一处给出推算值' if evidence.derived_data == 'label' else '禁止补算缺失指标，只列原文明确提供值并说明缺失'))
    if evidence.citation_template:
        lines.append(
            f"- 引用标记：使用「{evidence.citation_template}」格式标注来源，{{name}} 替换为实际来源文件名或知识库条目"
        )
    if evidence.min_sources_per_key_claim:
        lines.append(f"- 关键结论至少需要 {evidence.min_sources_per_key_claim} 个独立来源支持")
    if evidence.cross_validation:
        lines.append("- 关键数据必须交叉验证：不同来源给出的同一指标应相互核对，不一致时须说明")
    if evidence.conflict_policy:
        lines.append(f"- 来源冲突时的处理：{evidence.conflict_policy}")
    lines.append("- 禁止编造来源、数据与结论；没有来源支撑的内容必须显式标注为「未检索到依据」")
    return _join(lines)


def render_verification_brief(value: Any) -> str:
    """把验收项转成生成阶段的可执行要求，让模型有的放矢。"""
    skill = _skill(value)
    if skill is None:
        return ""
    check_texts = {
        "required_sections": "必填章节齐全，且章节标题与规范完全一致",
        "section_order": "章节出现顺序符合规范",
        "must_include": "每个章节覆盖其必须包含的要点",
        "required_data": "所有必须收集的数据字段都在正文中出现",
        "citations": "需引用章节带有符合模板的来源标记",
        "citation_sources": "引用标记中的来源名称必须能对应到实际检索到的来源",
        "unsupported_claims": "含预测/判断措辞的段落必须带引用，或明确标注为未检索到依据",
        "terminology": "统一使用规范中给出的术语写法",
        "banned_expressions": "不得出现规范中列出的禁用表达",
        "no_placeholder": "不得出现「待补充」「TODO」「此处省略」等占位内容",
        "min_length": "全文篇幅符合最少/最多字数要求",
    }
    lines = ["【验收要求】生成内容必须满足："]
    for check in skill.verification.checks:
        if check in check_texts:
            lines.append(f"- {check_texts[check]}")
    return _join(lines)


def render_instructions(value: Any) -> str:
    """SKILL.md 正文：执行流程与主要指令。"""
    skill = _skill(value)
    if skill is None or not skill.instructions:
        return ""
    return "【Skill 执行说明】\n" + skill.instructions


def render_references(value: Any) -> str:
    """references/ 下的术语表、详细规范与示例。"""
    skill = _skill(value)
    if skill is None or not skill.references:
        return ""
    blocks = ["【参考规范与术语】"]
    for item in skill.references:
        blocks.append(f"### {item.name}\n{item.content}")
    return "\n\n".join(blocks)


def render_retrieval_gaps(retrieval_report: Any) -> str:
    """把检索未覆盖的问题转成写作阶段必须显式标注的提示。"""
    if not isinstance(retrieval_report, dict):
        return ""
    missing = [
        question for question in retrieval_report.get("questions") or []
        if not question.get("covered")
    ]
    if not missing:
        return ""
    lines = ["【未检索到依据的研究问题】"]
    for question in missing:
        lines.append(f"- {question.get('question') or question.get('id')}")
    lines.append("写作时必须在对应位置明确标注「未检索到依据」，不得编造数据或来源。")
    return "\n".join(lines)


def render_full_brief(value: Any) -> str:
    """把执行说明、参考文档、写作、结构、格式、检索、证据与验收要求合成一个完整规范块。"""
    blocks = [
        render_instructions(value),
        render_references(value),
        render_task_brief(value),
        render_structure_brief(value),
        render_format_brief(value),
        render_research_brief(value),
        render_evidence_brief(value),
        render_verification_brief(value),
    ]
    return "\n\n".join(block for block in blocks if block)


def allowed_tools(value: Any) -> List[str]:
    """返回当前 Skill 允许的检索工具；未配置 Skill 时保持原有三工具集合。"""
    skill = _skill(value)
    if skill is None:
        return ["file", "knowledge", "end"]
    return list(skill.research.source.priority) + ["end"]


def citation_pattern(value: Any) -> Optional[re.Pattern]:
    """把引用模板编译成正则；模板缺失或不含 {name} 时返回 None。"""
    skill = _skill(value)
    if skill is None:
        return None
    template = skill.evidence.citation_template
    if not template or "{name}" not in template:
        return None
    head, tail = template.split("{name}", 1)
    return re.compile(re.escape(head) + r".+?" + re.escape(tail), re.DOTALL)


def citation_name_pattern(value: Any) -> Optional[re.Pattern]:
    """把引用模板编译成带捕获组的正则，第 1 组即来源名称。"""
    skill = _skill(value)
    if skill is None:
        return None
    template = skill.evidence.citation_template
    if not template or "{name}" not in template:
        return None
    head, tail = template.split("{name}", 1)
    return re.compile(re.escape(head) + r"(.+?)" + re.escape(tail), re.DOTALL)
