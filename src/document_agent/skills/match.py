"""Skill 自动匹配的确定性逻辑。

优先级由高到低：
1. 用户原话中出现 Skill 的 id 或展示名称（唯一命中才采用，多个命中视为歧义）；
2. 用户原话命中 `task.triggers`，取最长触发词；仍并列则视为歧义；
3. 意图识别模型给出的 Skill 名称（作为兜底，且必须是已启用的 Skill）；
4. 不启用 Skill。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .schema import DocumentSkill
from .store import SkillError, list_skills, load_skill


def _enabled_skills() -> List[DocumentSkill]:
    skills, _ = list_skills()
    return [skill for skill in skills if skill.enabled]


def match_skill_for_text(user_text: str, llm_hint: Optional[str] = None) -> Tuple[Optional[DocumentSkill], str]:
    """按确定性规则匹配 Skill，返回 (Skill, 匹配说明)。"""
    text = (user_text or "").strip()
    lowered = text.lower()
    skills = _enabled_skills()

    if text:
        # 1) 显式 id：按完整词边界匹配，避免单字符 id 命中普通英文单词
        id_matches = [
            skill for skill in skills
            if re.search(
                rf"(?<![a-z0-9_-]){re.escape(skill.id.lower())}(?![a-z0-9_-])",
                lowered,
            )
        ]
        id_matches = list({skill.id: skill for skill in id_matches}.values())
        if len(id_matches) == 1:
            return id_matches[0], f"用户原话提到 Skill id「{id_matches[0].id}」"
        if len(id_matches) > 1:
            ids = "、".join(skill.id for skill in id_matches)
            return None, f"用户原话同时提到多个 Skill id（{ids}），请用 --skill 明确指定"

        # 2) 展示名称：同名包含时取最长名称，避免「行业研究」抢走「行业研究报告」
        name_matches = [(len(skill.name), skill) for skill in skills if skill.name and skill.name in text]
        if name_matches:
            max_length = max(item[0] for item in name_matches)
            best = list({
                item[1].id: item[1]
                for item in name_matches
                if item[0] == max_length
            }.values())
            if len(best) == 1:
                return best[0], f"用户原话提到「{best[0].name}」（{best[0].id}）"
            ids = "、".join(skill.id for skill in best)
            return None, f"用户原话同时提到多个 Skill（{ids}），请用 --skill 明确指定"

        trigger_hits: List[Tuple[int, str, DocumentSkill]] = []
        for skill in skills:
            for trigger in skill.task.triggers:
                if trigger and trigger in text:
                    trigger_hits.append((len(trigger), trigger, skill))
        if trigger_hits:
            max_length = max(item[0] for item in trigger_hits)
            best = [item for item in trigger_hits if item[0] == max_length]
            best_skills = list({item[2].id: item[2] for item in best}.values())
            if len(best_skills) == 1:
                trigger = best[0][1]
                return best_skills[0], f"命中触发词「{trigger}」（{best_skills[0].id}）"
            ids = "、".join(skill.id for skill in best_skills)
            return None, f"触发词同时匹配多个 Skill（{ids}），请用 --skill 明确指定"

    if llm_hint:
        try:
            skill = load_skill(llm_hint)
        except SkillError:
            return None, f"意图识别建议的 Skill「{llm_hint}」未找到或已禁用，已忽略"
        return skill, f"采用意图识别建议「{skill.name}」（{skill.id}）"

    return None, "用户未指定 Skill，按默认流程执行"
