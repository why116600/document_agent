"""运行时 Skill 只读门面。

主入口（写作 / 改写）只允许通过本模块读取 Skill：
- 只按发布索引读取不可变快照，草稿启停不影响生产；
- 快照缺失或损坏时明确报错，绝不回退活动目录；
- 不暴露保存、删除、发布等写操作；
- 显式传入的临时配置文件路径只在 ``allow_unpublished_path=True`` 时放行（CLI 预览）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .publish import (
    PublishError,
    load_release,
    normalize_state,
    published_index,
    read_publish_state,
)
from .schema import DocumentSkill, SKILL_ID_PATTERN
from .store import SkillError, list_skills, load_skill, load_skill_dir, load_skill_file


class RuntimeSkillError(SkillError):
    """主入口不可用的 Skill；``code`` 供调用方区分原因。"""

    def __init__(self, message: str, code: str = "SKILL_UNAVAILABLE") -> None:
        super().__init__(message)
        self.code = code


def _published_skills() -> Tuple[List[DocumentSkill], List[Dict[str, str]]]:
    skills: List[DocumentSkill] = []
    diagnostics: List[Dict[str, str]] = []
    for skill_id, version in published_index():
        try:
            skills.append(load_release(skill_id, version))
        except (SkillError, PublishError) as exc:
            diagnostics.append({
                "skill_id": skill_id,
                "version": str(version),
                "code": getattr(exc, "code", "RELEASE_ERROR"),
                "detail": str(exc),
            })
    return skills, diagnostics


def list_runtime_skills() -> List[DocumentSkill]:
    """列出主入口可用的 Skill：已发布、未删除、生产启用。"""
    skills, _ = _published_skills()
    return skills


def runtime_diagnostics() -> List[Dict[str, str]]:
    """返回不可用条目：快照加载失败、从未发布、生产停用或已删除。"""
    _, diagnostics = _published_skills()
    published = {skill_id for skill_id, _ in published_index()}
    try:
        skills, _ = list_skills()
    except SkillError:
        return diagnostics
    for skill in skills:
        if skill.id in published:
            continue
        state = _state_by_id(skill.id)
        if state is None:
            diagnostics.append({
                "skill_id": skill.id,
                "version": "",
                "code": "NOT_PUBLISHED",
                "detail": f"Skill「{skill.name}」尚未纳入发布流程，请执行 migrate/publish",
            })
            continue
        normalized = normalize_state(state)
        if normalized.get("lifecycle_status") == "deleted":
            diagnostics.append({
                "skill_id": skill.id, "version": "", "code": "SKILL_DELETED",
                "detail": "Skill 已删除，不参与运行时匹配",
            })
        elif normalized.get("runtime_enabled") is False:
            diagnostics.append({
                "skill_id": skill.id, "version": "", "code": "SKILL_DISABLED",
                "detail": "生产已停用，不参与运行时匹配",
            })
        elif not normalized.get("published_version"):
            diagnostics.append({
                "skill_id": skill.id, "version": "", "code": "NOT_PUBLISHED",
                "detail": "只有草稿，没有发布快照",
            })
    return diagnostics


def list_runtime_summaries() -> List[Dict[str, Any]]:
    """给意图识别使用的摘要，不包含完整指令与参考附件。"""
    return [
        {
            "id": skill.id,
            "name": skill.name,
            "enabled": True,
            "doc_type": skill.task.doc_type,
            "description": skill.description,
            "purpose": skill.task.purpose,
            "triggers": skill.task.triggers,
        }
        for skill in list_runtime_skills()
    ]


def load_runtime_skill(ref: str, allow_unpublished_path: bool = False) -> DocumentSkill:
    """按 id/名称/路径加载 Skill；已安装 Skill 必须有可用的发布快照。"""
    raw = str(ref or "").strip()
    if not raw:
        raise RuntimeSkillError("未提供 Skill 名称或 id", "EMPTY_REF")

    path = Path(raw).expanduser()
    if path.exists():
        if not allow_unpublished_path:
            raise RuntimeSkillError(
                f"不允许按路径加载未发布 Skill：{raw}；请发布后按 id 使用", "UNPUBLISHED_PATH"
            )
        return _load_source(path)

    try:
        skill = load_skill(raw, allow_disabled=True)
    except SkillError as exc:
        state = _state_by_id(raw)
        if state is not None and normalize_state(state).get("lifecycle_status") == "deleted":
            raise RuntimeSkillError(f"Skill「{raw}」已删除，主入口不可使用", "SKILL_DELETED") from exc
        raise RuntimeSkillError(str(exc), "SKILL_NOT_FOUND") from exc

    try:
        state = read_publish_state(skill.id)
    except PublishError as exc:
        raise RuntimeSkillError(str(exc), exc.code) from exc
    if state is None:
        raise RuntimeSkillError(
            f"Skill「{skill.name}」（{skill.id}）尚未纳入发布流程，主入口不可使用", "NOT_PUBLISHED"
        )
    normalized = normalize_state(state)
    if normalized.get("lifecycle_status") == "deleted":
        raise RuntimeSkillError(f"Skill「{skill.name}」（{skill.id}）已删除，主入口不可使用", "SKILL_DELETED")
    if normalized.get("runtime_enabled") is False:
        raise RuntimeSkillError(f"Skill「{skill.name}」（{skill.id}）已停用，主入口不可使用", "SKILL_DISABLED")
    version = normalized.get("published_version")
    if not version:
        raise RuntimeSkillError(f"Skill「{skill.name}」（{skill.id}）尚未发布，主入口不可使用", "NOT_PUBLISHED")
    try:
        return load_release(skill.id, int(version))
    except PublishError as exc:
        raise RuntimeSkillError(str(exc), exc.code) from exc


def _state_by_id(raw: str) -> Optional[Dict[str, Any]]:
    """按 id 猜测发布态，用于在草稿已被删除时给出准确错误。"""
    candidate = raw.strip().lower()
    if not SKILL_ID_PATTERN.match(candidate):
        return None
    try:
        return read_publish_state(candidate)
    except PublishError:
        return None


def _load_source(path: Path) -> DocumentSkill:
    return load_skill_dir(path) if path.is_dir() else load_skill_file(path)
