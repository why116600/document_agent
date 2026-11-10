"""Skill 自动匹配的确定性逻辑。

优先级由高到低：
1. 用户原话中出现 Skill 的 id 或展示名称（唯一命中才采用）；
2. 用户原话命中 `task.triggers`，取最长触发词；
3. 意图识别模型给出的 Skill id（兜底）；
4. 不启用 Skill。

另外支持：use_skill=False 时整体不启用；excluded_skills 排除指定 id；
task_type 过滤掉适用任务不符的规范。多个候选无法区分时默认返回
(None, 原因) 让调用方按普通流程继续；只有明确要求必须使用规范时才报错。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .schema import DocumentSkill
from .store import SkillError, list_skills, load_skill


def _enabled_skills() -> List[DocumentSkill]:
    skills, _ = list_skills()
    return [skill for skill in skills if skill.enabled]


def match_skill_for_text(user_text: str, llm_hint: Optional[str] = None, *,
                         use_skill: Optional[bool] = None, excluded_skills=(),
                         task_type: Optional[str] = None) -> Tuple[Optional[DocumentSkill], str]:
    """按确定性规则匹配 Skill，返回 (Skill, 匹配说明)。"""
    if use_skill is False:
        return None, "用户要求不使用 Skill，按普通流程执行"
    text = (user_text or "").strip()
    lowered = text.lower()
    excluded = {str(item).strip().lower() for item in excluded_skills}
    skills = [skill for skill in _enabled_skills() if skill.id.lower() not in excluded]
    compatible = [s for s in skills if not task_type or s.task.doc_type in ('any', task_type)]

    def ambiguous(candidates, *, strict=False):
        names = '、'.join(f'{s.name}（{s.id}）' for s in candidates)
        message = f'需求匹配多个 Skill：{names}。请在需求中说明要采用哪一项规范的名称或 id。'
        if strict:
            raise SkillError(message)
        return None, message

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
            return ambiguous(id_matches, strict=use_skill is True)

        # 2) 展示名称：同名包含时取最长名称，避免「行业研究」抢走「行业研究报告」
        name_matches = [(len(skill.name), skill) for skill in skills if skill.name and skill.name in text]
        if name_matches:
            max_length = max(item[0] for item in name_matches)
            best = list({
                item[1].id: item[1]
                for item in name_matches
                if item[0] == max_length
            }.values())
            suitable = [skill for skill in best if skill in compatible]
            best = suitable or best
            if len(best) == 1:
                return best[0], f"用户原话提到「{best[0].name}」（{best[0].id}）"
            return ambiguous(best, strict=use_skill is True)

        trigger_hits: List[Tuple[int, str, DocumentSkill]] = []
        for skill in compatible:
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
            return ambiguous(best_skills, strict=use_skill is True)

    if llm_hint:
        try:
            skill = load_skill(llm_hint)
        except SkillError:
            if use_skill is True:
                raise SkillError(f'用户要求使用的规范「{llm_hint}」不存在或未启用，请说明已配置的规范名称。') from None
            return None, f"意图识别建议的 Skill「{llm_hint}」未找到或已禁用，已忽略"
        if skill.id.lower() in excluded or (task_type and skill.task.doc_type not in ('any', task_type)):
            if use_skill is True:
                raise SkillError(f'Skill「{skill.name}」与排除条件或当前任务类型不符，请说明要采用的规范。')
            return None, '意图建议与排除条件或任务类型不符，未采用'
        return skill, f"采用意图识别建议「{skill.name}」（{skill.id}）"

    if use_skill is True:
        raise SkillError('用户要求使用写作规范，但未找到唯一适用的已启用 Skill。请说明规范名称；尚未配置时可用 console_app skill new 创建。')
    return None, "未匹配到适用的已启用 Skill，按普通流程执行"


def resolve_skill(user_text, *, explicit=None, llm_hint=None, use_skill=None,
                  excluded_skills=(), task_type=None):
    """CLI与工作流共用选择规则；显式参数覆盖自然语言选择。"""
    if explicit:
        skill, reason = load_skill(explicit), f'显式指定 --skill={explicit}'
    else:
        skill, reason = match_skill_for_text(user_text, llm_hint, use_skill=use_skill,
                                             excluded_skills=excluded_skills, task_type=task_type)
    if skill and task_type and skill.task.doc_type not in ('any', task_type):
        raise SkillError(f'Skill「{skill.name}」适用于{skill.task.doc_type}，与当前{task_type}任务不符。')
    return skill, reason
