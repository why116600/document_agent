"""自然语言生成/修改 Skill（仅管理域使用）。

运行时（写作入口）不会导入本模块；只有 ``/admin/api`` 与管理 CLI 会调用。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..schema import DocumentSkill, explicit_source_restriction
from ..templates import draft_skill_prompt

MAX_DESCRIPTION_CHARS = 4000


class DraftGenerationError(Exception):
    """模型未返回合法 Skill 配置。"""


def _default_llm():
    from document_agent.agent.llm_client import get_deepseek_llm

    return get_deepseek_llm()


def _invoke(llm, prompt: str):
    from document_agent.agent.llm_client import llm_invoke

    return llm_invoke(llm, prompt, retry=3)


def _extract(text: str):
    from document_agent.agent.llm_client import extract_json_from_text

    return extract_json_from_text(text, return_all=True) or []


def generate_skill(
    description: str,
    *,
    skill_id: Optional[str] = None,
    existing: Optional[DocumentSkill] = None,
    llm=None,
    sources=None,
) -> DocumentSkill:
    """把自然语言需求生成/补丁为合法 Skill；不直接落盘。"""
    from .service import PATCHABLE_KEYS, _critical_lists_emptied, _merge_patch

    text = (description or "").strip()
    if not text:
        raise DraftGenerationError("需求描述不能为空")
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise DraftGenerationError(f"需求描述不能超过 {MAX_DESCRIPTION_CHARS} 字")

    try:
        restriction = explicit_source_restriction(text)
    except ValueError as exc:
        raise DraftGenerationError(str(exc)) from exc
    if sources and restriction and sources != restriction:
        raise DraftGenerationError("来源限制与需求描述冲突")
    required_sources = sources or restriction

    llm = llm or _default_llm()
    base_prompt = draft_skill_prompt(text, skill_id, existing)
    if required_sources:
        base_prompt += f"\nresearch.source.priority 必须等于 {json.dumps(required_sources)}。"

    prompt, error = base_prompt, ""
    for _ in range(3):
        response = _invoke(llm, prompt)
        candidates = _extract(str(getattr(response, "content", "") or ""))
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if not isinstance(data, dict) or not data:
                    raise ValueError("需要非空 JSON 对象")
                if existing is not None:
                    unexpected = set(data) - PATCHABLE_KEYS
                    if unexpected:
                        raise ValueError(f"局部修改不允许改这些字段：{sorted(unexpected)}")
                    before = existing.to_config_dict()
                    data = _merge_patch(before, data)
                    skill = DocumentSkill.model_validate(data)
                    emptied = _critical_lists_emptied(before, skill.to_config_dict())
                    if emptied:
                        raise ValueError(
                            f"修改后这些关键清单被清空：{'、'.join(emptied)}；请明确说明要改的内容"
                        )
                else:
                    skill = DocumentSkill.model_validate(data)
                if skill_id and skill.id != skill_id:
                    raise ValueError(f"id 必须为 {skill_id}")
                if required_sources and skill.research.source.priority != required_sources:
                    raise ValueError(f"research.source.priority 必须等于 {required_sources}")
                if existing is None:
                    if skill.template or skill.template_path or skill.references:
                        raise ValueError("创建 Skill 不创建模板或参考附件，请先将 template 留空")
                    skill = skill.model_copy(update={"version": 1, "created_at": None, "updated_at": None})
                    skill.instructions = "# 执行说明\n\n需求来源：" + text + "\n\n" + skill.instructions
                    skill = DocumentSkill.model_validate(skill.model_dump())
                return skill
            except (ValueError, TypeError) as exc:
                error = str(exc)[:800]
        prompt = (
            base_prompt
            + "\n【上次输出未通过校验】"
            + (error or "未找到 JSON 对象")
            + "\n请重新返回所需 JSON，不要解释。"
        )
    raise DraftGenerationError("连续 3 次未能得到合法 Skill：" + (error or "模型未返回 JSON"))


def skill_to_draft(skill: DocumentSkill) -> Dict[str, Any]:
    """把生成结果整理成管理 API 的返回结构。"""
    return {
        "config": skill.to_config_dict(),
        "instructions": skill.instructions,
        "template": skill.template,
        "references": [item.model_dump() for item in skill.references],
    }
