"""用户自定义文档 Skill：数据结构、加载、渲染与模板。"""

from .schema import (
    AVAILABLE_SOURCES,
    VERIFICATION_CHECKS,
    DocumentSkill,
    SkillValidationError,
    coerce_skill,
)
from .match import match_skill_for_text
from .plan import build_retrieval_plan, finalize_report, new_progress, record_assessment
from .store import (
    SkillError,
    list_skill_summaries,
    list_skills,
    load_skill,
    load_skill_dir,
    load_skill_file,
    resolve_skill_dir,
    save_skill,
    save_skill_package,
)

__all__ = [
    "AVAILABLE_SOURCES",
    "VERIFICATION_CHECKS",
    "DocumentSkill",
    "SkillError",
    "SkillValidationError",
    "build_retrieval_plan",
    "coerce_skill",
    "finalize_report",
    "list_skill_summaries",
    "list_skills",
    "load_skill",
    "load_skill_dir",
    "load_skill_file",
    "match_skill_for_text",
    "new_progress",
    "record_assessment",
    "resolve_skill_dir",
    "save_skill",
    "save_skill_package",
]
