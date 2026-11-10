"""Skill 模板、SKILL.md 正文模板与「自然语言生成 Skill」所需的提示词。"""

from __future__ import annotations

import json
from typing import Optional

from .schema import (
    AVAILABLE_SOURCES,
    DocumentSkill,
    EvidenceSpec,
    FormatSpec,
    KnowledgeRetrievalSpec,
    LengthTarget,
    ResearchSpec,
    SectionSpec,
    SourcePolicy,
    StructureSpec,
    TaskSpec,
    VerificationSpec,
)

TEMPLATE_BODY = """# 执行说明

## 适用场景

说明本 Skill 适用于哪类文档与哪类任务（新建 / 改写），以及用户通常会怎样描述需求。

## 执行流程

1. 先确认文档用途、读者与硬性章节；
2. 按「检索规范」收集必需数据，并在正文标注来源；
3. 逐章写作，遵守章节顺序、格式与术语要求；
4. 输出前自检：章节齐全、要点覆盖、引用可追溯、无占位内容。

## 主要指令

- 结论先行，先给判断再给依据；
- 所有关键数据必须带来源标记；
- 没有检索到依据的内容必须显式标注，不得编造。
"""


def build_template_skill(skill_id: str, name: Optional[str] = None) -> DocumentSkill:
    """生成一份可直接编辑的最小可用 Skill 模板。"""
    return DocumentSkill(
        id=skill_id,
        name=name or skill_id,
        description="示例模板：请按实际文档类型修改用途、章节、检索目标与验收要求",
        task=TaskSpec(
            doc_type="any",
            triggers=["写一份文档", "起草规范文档"],
            purpose="说明文档要达到的业务目的",
            audience="说明文档给谁看",
            tone="正式、客观、结论先行",
            professionalism="专业严谨",
            length_target=LengthTarget(min_chars=1500),
            terminology=["统一使用文档中给出的术语写法"],
            banned_expressions=["大概", "可能吧", "众所周知"],
        ),
        structure=StructureSpec(
            order="strict",
            numbering="一、",
            output_format="以段落为主，关键数据用表格呈现",
            format=FormatSpec(
                body_font="宋体",
                body_size=12,
                heading_font="黑体",
                heading_sizes={1: 16, 2: 14},
                heading_bold=True,
                line_spacing=1.5,
            ),
            sections=[
                SectionSpec(
                    key="overview",
                    title="概述",
                    level=1,
                    guidance="说明文档背景、目的与适用范围",
                    must_include=["背景", "目的"],
                    evidence_types=["背景资料"],
                ),
                SectionSpec(
                    key="body",
                    title="主要内容",
                    level=1,
                    guidance="按主题展开论述，关键数据需给出处",
                    evidence_required=True,
                    min_citations=1,
                    evidence_types=["数据指标"],
                ),
                SectionSpec(
                    key="conclusion",
                    title="结论与建议",
                    level=1,
                    guidance="给出可执行的结论与后续建议",
                ),
            ],
        ),
        research=ResearchSpec(
            questions=["围绕文档主题需要回答的核心问题"],
            data_fields=["关键指标名称"],
            time_range="最近三年",
            max_rounds=4,
            per_question_attempts=2,
            source=SourcePolicy(priority=["file", "knowledge"]),
            knowledge=KnowledgeRetrievalSpec(top_k=8, min_similarity=0.2, max_expansions=5),
            credibility_rule="优先采用正式发布、可追溯的资料",
        ),
        evidence=EvidenceSpec(
            citation_template="【来源：{name}】",
            min_sources_per_key_claim=1,
            cross_validation=True,
            conflict_policy="优先采用来源优先级更高的资料，并在文中标注冲突",
        ),
        verification=VerificationSpec(
            checks=[
                "required_sections",
                "section_order",
                "must_include",
                "citations",
                "citation_sources",
                "banned_expressions",
                "no_placeholder",
                "min_length",
            ],
        ),
    )


def skill_example_json() -> str:
    """生成入口使用保守示例，避免把可编辑模板中的示例限制当成默认要求。"""
    template = DocumentSkill(
        id="example_skill", name="示例规范",
        structure=StructureSpec(sections=[SectionSpec(title="用户要求的章节")]),
        recommendations=["篇幅按实际信息量决定，用户未限定时不必预设字数", "资料包含相关指标时可补充呈现，未取得时不强求"],
    )
    return json.dumps(template.to_config_dict(), ensure_ascii=False, indent=2)


def draft_skill_prompt(description: str, skill_id: Optional[str] = None, existing=None) -> str:
    """构造「用户自然语言描述 → Skill JSON」的提示词。"""
    id_line = f"id 使用「{skill_id}」，" if skill_id else "id 依据主题取英文小写下划线或短横线，"
    identity = f"- {id_line}name 为中文展示名；version 固定为 1；\n" if existing is None else (
        '- 只返回局部JSON补丁，不得返回id、version、created_at、updated_at、references、template_path；'
        '未要求修改的字段不输出，字典递归合并，不要输出 null（不允许删除字段），'
        '数组需返回修改后的完整数组并保留其他项；不得把章节、检索、证据、验收等清单整块置空。\n')
    intro = '请把用户需求转换成一份完整Skill JSON配置。' if existing is None else '请仅按用户要求修改现有Skill，返回局部JSON补丁。'
    example = ('【字段示例，内容按需求重新生成】\n' + skill_example_json() if existing is None else
               '【当前规范，未要求变更的内容必须保留】\n' + json.dumps(existing.to_config_dict(), ensure_ascii=False))
    return (
        f"你是文档工作流配置助手。{intro}\n"
        "只输出一个 JSON 对象，不要输出任何解释、Markdown 代码块或多余文字。\n\n"
        f"【用户需求】\n{description}\n\n"
        "【字段约束】\n"
        f"{identity}"
        "【硬性要求与建议】\n"
        "- 现有结构化约束字段表示硬性要求。只将用户明确提出的要求，或用户明确指定且已提供内容的规范写入这些字段。\n"
        "- 模型按经验补充的篇幅、章节字数、指标、术语、格式、交叉验证等，放入顶层recommendations字符串数组；"
        "这些在Skill中就是推荐默认值，仅在用户和规范没有明确指定该项时采用，不得写成硬性约束；"
        "用户已有明确要求时以明确要求为准。推荐值不计入验收，缺失推荐指标或偏离推荐字数不能判错，也不能编造数据补齐。\n"
        "- 用户未限定篇幅时task.length_target留空，section.min_body_chars保持1以防空章；"
        "未要求的必填字段、数值指标、禁用词和语义要求用空数组，cross_validation保持false。\n"
        "- research.questions可以依据用户要求的主题组织检索问题，但不得夹带模型新增的必填指标；"
        "可选指标仅辅助检索，不作为问题已覆盖的必要条件。\n"
        "- instructions、guidance等文字中也不得把建议复述为必须；不要把模型经验声称为已核实的行业标准。\n"
        "- 修改已有规范时，保留未涉及的硬性要求及recommendations；只有用户明确要求调整时才将建议升为硬性要求或反向调整。\n"
        "- 字段示例仅说明结构，不得照搬示例内容或数值；用户未限定的其它业务条件应留空。\n"
        "- task.doc_type 取 new / rewrite / any；task.purpose、audience、tone、professionalism 用中文描述；\n"
        "- task.triggers 为触发词数组；task.terminology 为统一术语数组；task.banned_expressions 为禁用表达数组；\n"
        "- task.length_target 可含 min_chars / max_chars，均为整数；\n"
        "- structure.order 取 strict / flexible；sections 为章节数组，每项含 title（必填）、level(1~4)、"
        "required、must_include、evidence_types、evidence_required、min_citations、guidance；\n"
        "- must_include 是必须逐字出现的短关键词，自然语言内容要求放在 requirements 数组；"
        "章节还可含 min_body_chars 和 data_fields（必须属于 research.data_fields），保证非空章和指标覆盖；\n"
        "- structure.format 可含 body_font、body_size、heading_font、heading_sizes、heading_bold、"
        "line_spacing、first_line_indent；\n"
        "- research.questions 为必须回答的研究问题数组；research.data_fields 为必须收集的字段数组；\n"
        "- research.numeric_fields 是 data_fields 的子集，列出必须有实际数值的字段；政策等定性字段不列入；\n"
        "- research.field_aliases 可为字段配置等价写法，键必须在 data_fields 中，值为字符串数组；\n"
        "- research.time_range、region、credibility_rule 为自然语言描述；research.max_rounds 为 1~10 的整数；\n"
        "- research.per_question_attempts 为 1~5 的整数，表示单个问题最多尝试几次；\n"
        "- max_rounds 不得小于问题数；建议覆盖问题数乘以 per_question_attempts（最多10），超限时减少问题或尝试次数；\n"
        "- 用户要求仅使用附件/用户文件时 source.priority 必须为 [\"file\"]，不得保留 knowledge 默认值；\n"
        f"- research.source.priority 只能从 {AVAILABLE_SOURCES} 中取值，按优先级排列；\n"
        "- research.knowledge 可含 top_k(1~50)、min_similarity(0~1)、expand_neighbors、max_expansions(1~50)；\n"
        "- research.file 可含 max_chars（0 表示不限制）；research.query_expansion 为布尔值；\n"
        "- research.on_empty 取 rewrite_query / fallback_source / mark_missing；\n"
        "- research.stop_after_no_new_evidence 为0~10的整数，0关闭，正数表示连续空结果停止阈值；\n"
        "- research.stop_when 是可选自然语言停止条件，每轮由模型判断；确定的空结果阈值请用上一个字段；\n"
        "- evidence.citation_template 必须包含 {name} 占位符，默认「【来源：{name}】」；\n"
        "- evidence.min_sources_per_key_claim 为 0~10 的整数；evidence.cross_validation 为布尔值；\n"
        "- evidence.derived_data 取 label（允许计算但标明来源/公式）或 forbid（禁止补算）；\n"
        "- instructions 是执行说明正文；template 只能引用已有包内模板，不创建或改写参考附件、模板文件；\n"
        "- verification.checks 只能从 required_sections / section_order / must_include / required_data / "
        "citations / citation_sources / unsupported_claims / banned_expressions / "
        "no_placeholder / min_length 中取值；术语写法统一由语义评估核对，不要写进 checks；\n"
        "- verification.max_section_attempts 为1~3，控制每章生成及修复的总次数；\n"
        "- verification.semantic_review 为布尔值；verification.on_failure 固定为 report；\n"
        "- 禁止出现上述字段之外的键名。\n\n"
        f"{example}\n"
    )
