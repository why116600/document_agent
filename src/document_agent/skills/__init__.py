"""用户自定义文档 Skill：数据结构、加载、渲染与模板。"""

from .schema import (
    AVAILABLE_SOURCES,
    VERIFICATION_CHECKS,
    DocumentSkill,
    SkillValidationError,
    coerce_skill,
)
from .match import match_skill_for_text, resolve_skill
from .plan import build_retrieval_plan, finalize_report, new_progress, record_assessment
from .store import (
    SkillError,
    list_skill_summaries,
    list_skills,
    load_skill,
    load_skill_dir,
    load_skill_file,
    read_skill_for_update,
    resolve_skill_dir,
    save_skill,
    save_skill_package,
    save_skill_update,
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
    "read_skill_for_update",
    "record_assessment",
    "resolve_skill",
    "resolve_skill_dir",
    "save_skill",
    "save_skill_package",
    "save_skill_update",
]
