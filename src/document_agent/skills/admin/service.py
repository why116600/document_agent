"""Skill 管理服务：结构化 CRUD、启停、发布、回滚、附件与审计。

设计边界：
- 供管理 CLI 与 ``/admin/api`` 使用；
- 不解析自然语言，不调用模型；
- 所有写操作记录审计，删除默认软删除。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from ..publish import (
    PublishConflict,
    PublishError,
    clear_publish_artifacts,
    list_versions,
    mark_deleted,
    mark_draft,
    migrate_skills,
    migration_plan,
    normalize_state,
    publish_skill,
    read_publish_state,
    restore_draft,
    rollback_skill,
    set_runtime_enabled,
)
from ..schema import DocumentSkill
from ..store import (
    SkillError,
    _atomic_write,
    delete_skill,
    list_skills,
    load_skill_dir,
    load_skill_file,
    locate_skill_source,
    read_skill_for_update,
    resolve_skill_dir,
    save_skill,
    save_skill_package,
    save_skill_update,
)
from .audit import audit, current_operator

MAX_REFERENCE_BYTES = 1_000_000
MAX_TEMPLATE_BYTES = 20_000_000
REFERENCE_SUFFIXES = {".md", ".markdown", ".txt"}

PATCHABLE_KEYS = {
    "name", "description", "enabled", "created_by", "task", "structure",
    "research", "evidence", "verification", "instructions", "template",
    "recommendations",
}

_CRITICAL_LISTS = (
    ("structure", "sections"),
    ("research", "questions"),
    ("research", "data_fields"),
    ("verification", "checks"),
)


class AdminError(Exception):
    """管理操作失败。"""


class AdminNotFound(AdminError):
    """Skill 不存在或无法唯一定位。"""


class AdminConflict(AdminError):
    """状态冲突：版本过期、重复创建、附件不支持等。"""


class AdminValidationError(AdminError):
    """配置或上传内容不合法。"""


def _merge_patch(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """只合并指定键；不允许用 null 删除字段。"""
    result = dict(current)
    for key, value in patch.items():
        if value is None:
            raise AdminValidationError(f"局部修改不允许用 null 删除字段：{key}")
        if isinstance(value, dict):
            base = result.get(key) if isinstance(result.get(key), dict) else {}
            result[key] = _merge_patch(base, value)
        else:
            result[key] = value
    return result


def _critical_lists_emptied(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    emptied: List[str] = []
    for block, field in _CRITICAL_LISTS:
        previous = (before.get(block) or {}).get(field) or []
        current = (after.get(block) or {}).get(field) or []
        if previous and not current:
            emptied.append(f"{block}.{field}")
    return emptied


class SkillAdminService:
    """管理入口的业务实现，路由与 CLI 共用。"""

    # ------------------------------------------------------------------ 查询
    def list(
        self,
        include_disabled: bool = True,
        keyword: str = "",
        page: int = 1,
        size: int = 50,
    ) -> Dict[str, Any]:
        skills, errors = list_skills()
        lowered = (keyword or "").strip().lower()
        items = []
        for skill in skills:
            if not include_disabled and not skill.enabled:
                continue
            haystack = f"{skill.id} {skill.name} {skill.description}".lower()
            if lowered and lowered not in haystack:
                continue
            items.append(self._summary(skill))
        total = len(items)
        start = max(0, (page - 1) * size)
        return {
            "items": items[start:start + size],
            "total": total,
            "page": page,
            "size": size,
            "errors": errors,
        }

    def get(self, skill_id: str) -> Dict[str, Any]:
        source = self._locate(skill_id)
        skill = self._load(source)
        data = skill.to_config_dict()
        data["references"] = [item.model_dump() for item in skill.references]
        data["template_path"] = skill.template_path
        data["source"] = str(source)
        data["state"] = self._state_view(skill.id)
        data["versions"] = list_versions(skill.id)
        return data

    def validate(self, skill_id: str) -> Dict[str, Any]:
        try:
            source = self._locate(skill_id)
            skill = self._load(source, allow_disabled=True)
        except SkillError as exc:
            return {"id": skill_id, "valid": False, "error": str(exc)}
        return {
            "id": skill.id,
            "valid": True,
            "summary": skill.summary(),
            "source": str(source),
        }

    # ------------------------------------------------------------------ 写入
    def create(
        self,
        config: Dict[str, Any],
        instructions: str = "",
        publish: bool = False,
        *,
        single: bool = False,
        as_yaml: bool = False,
        root: Optional[str | Path] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        try:
            skill = DocumentSkill.model_validate(config)
        except ValidationError as exc:
            raise AdminValidationError(self._format_validation(exc)) from exc

        actor = current_operator()
        base = Path(root).expanduser().resolve() if root else None
        if single:
            target = (base or resolve_skill_dir()) / f"{skill.id}{'.yaml' if as_yaml else '.json'}"
            if target.exists() and not force:
                raise AdminConflict(f"目标文件已存在：{target}")
            save_skill(skill, out_path=target)
            self._clear_publish_artifacts(skill.id)
            mark_draft(skill.id, skill.version)
            if publish:
                publish_skill(skill.id, actor=actor)
        else:
            save_skill_package(
                skill,
                instructions=instructions or skill.instructions,
                root=base,
                force=force,
            )
            # 同名 Skill 可能来自软删除；创建成功后清理旧发布态
            self._clear_publish_artifacts(skill.id)
            mark_draft(skill.id, skill.version)
            if publish:
                publish_skill(skill.id, actor=actor)

        audit(actor, "skill.create", skill.id, version=skill.version)
        return self.get(skill.id)

    # ------------------------------------------------------------------ 自然语言
    def draft_create(
        self,
        description: str,
        skill_id: Optional[str] = None,
        publish: bool = False,
        *,
        llm=None,
        root: Optional[str | Path] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        from .drafting import DraftGenerationError, generate_skill

        try:
            skill = generate_skill(description, skill_id=skill_id, llm=llm)
        except DraftGenerationError as exc:
            raise AdminValidationError(str(exc)) from exc
        result = self.create(
            skill.to_config_dict(),
            instructions=skill.instructions,
            publish=publish,
            root=root,
            force=force,
        )
        audit(current_operator(), "skill.draft.create", skill.id, version=skill.version)
        return result

    def draft_update(
        self,
        skill_id: str,
        description: str,
        expected_version: Optional[int] = None,
        *,
        llm=None,
    ) -> Dict[str, Any]:
        from .drafting import DraftGenerationError, generate_skill

        source, current, snapshot = self._read_for_update(skill_id)
        if expected_version is not None and current.version != expected_version:
            raise AdminConflict(
                f"版本已变化：期望 v{expected_version}，当前 v{current.version}"
            )
        try:
            updated = generate_skill(
                description,
                skill_id=current.id,
                existing=current,
                llm=llm,
            )
        except DraftGenerationError as exc:
            raise AdminValidationError(str(exc)) from exc

        before, after = current.to_config_dict(), updated.to_config_dict()
        changed = sorted(key for key in before.keys() | after.keys() if after.get(key) != before.get(key))
        if not changed:
            raise AdminValidationError("未产生配置变更，请说明具体要修改的内容")

        updated, backup = save_skill_update(source, updated, snapshot)
        mark_draft(updated.id, updated.version)
        audit(current_operator(), "skill.draft.update", updated.id, version=updated.version)
        result = self.get(updated.id)
        result["backup"] = str(backup)
        result["changed_fields"] = changed
        return result

    def draft_preview(
        self,
        description: str,
        skill_id: Optional[str] = None,
        existing_id: Optional[str] = None,
        *,
        llm=None,
    ) -> Dict[str, Any]:
        from .drafting import DraftGenerationError, generate_skill, skill_to_draft

        existing = None
        if existing_id:
            source, existing, _ = self._read_for_update(existing_id)
            skill_id = existing.id
        try:
            skill = generate_skill(
                description,
                skill_id=skill_id,
                existing=existing,
                llm=llm,
            )
        except DraftGenerationError as exc:
            raise AdminValidationError(str(exc)) from exc
        return skill_to_draft(skill)

    def update(
        self,
        skill_id: str,
        patch: Dict[str, Any],
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        unexpected = set(patch) - PATCHABLE_KEYS
        if unexpected:
            raise AdminValidationError(f"不允许修改这些字段：{sorted(unexpected)}")

        return self._apply_patch(skill_id, patch, expected_version)

    def replace(
        self,
        skill_id: str,
        config: Dict[str, Any],
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """PUT 语义：接收完整配置，过滤身份、版本与运行时派生字段。"""
        incoming = dict(config)
        declared_id = incoming.pop("id", None)
        if declared_id and str(declared_id) != skill_id:
            raise AdminValidationError("请求体中的 id 与路径不一致")
        for key in ("version", "created_at", "updated_at", "references", "template_path"):
            incoming.pop(key, None)
        unexpected = set(incoming) - PATCHABLE_KEYS
        if unexpected:
            raise AdminValidationError(f"不允许修改这些字段：{sorted(unexpected)}")
        return self._apply_patch(skill_id, incoming, expected_version)

    def _apply_patch(
        self,
        skill_id: str,
        patch: Dict[str, Any],
        expected_version: Optional[int],
    ) -> Dict[str, Any]:
        source, current, snapshot = self._read_for_update(skill_id)
        if expected_version is not None and current.version != expected_version:
            raise AdminConflict(
                f"版本已变化：期望 v{expected_version}，当前 v{current.version}"
            )
        merged = _merge_patch(current.to_config_dict(), patch)
        try:
            updated = DocumentSkill.model_validate(merged)
        except ValidationError as exc:
            raise AdminValidationError(self._format_validation(exc)) from exc

        emptied = _critical_lists_emptied(current.to_config_dict(), updated.to_config_dict())
        if emptied:
            raise AdminValidationError(
                "修改后这些关键清单被清空：" + "、".join(emptied)
            )

        updated, backup = save_skill_update(source, updated, snapshot)
        mark_draft(updated.id, updated.version)
        audit(current_operator(), "skill.update", updated.id, version=updated.version)
        result = self.get(updated.id)
        result["backup"] = str(backup)
        return result

    def delete(self, skill_id: str, purge: bool = False) -> Dict[str, Any]:
        source = self._locate(skill_id)
        actor = current_operator()
        if purge:
            trash = delete_skill(skill_id, purge=True)
            clear_publish_artifacts(skill_id)
        else:
            # 先写墓碑再搬走草稿，避免软删除后仍被发布索引返回
            mark_deleted(skill_id, actor=actor)
            trash = delete_skill(skill_id, purge=False)
        audit(
            current_operator(),
            "skill.delete",
            skill_id,
            version=None,
            detail="purge" if purge else "soft",
        )
        return {"id": skill_id, "source": str(source), "trash": str(trash), "purged": purge}

    def set_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        """生产启停开关：只改发布态，不改草稿配置，也不触发新版本。"""
        source = self._locate(skill_id)
        skill = self._load(source)
        set_runtime_enabled(skill.id, enabled)
        audit(
            current_operator(),
            "skill.enable" if enabled else "skill.disable",
            skill.id,
            version=skill.version,
        )
        result = self._summary(skill)
        result["runtime_enabled"] = bool(enabled)
        return result

    # ------------------------------------------------------------------ 发布
    def publish(self, skill_id: str, force: bool = False) -> Dict[str, Any]:
        try:
            result = publish_skill(skill_id, force=force, actor=current_operator())
        except PublishConflict as exc:
            raise AdminConflict(str(exc)) from exc
        except PublishError as exc:
            raise AdminError(str(exc)) from exc
        audit(current_operator(), "skill.publish", skill_id, version=result["published_version"])
        return result

    def rollback(self, skill_id: str, version: int) -> Dict[str, Any]:
        try:
            result = rollback_skill(skill_id, version, actor=current_operator())
        except PublishError as exc:
            raise AdminError(str(exc)) from exc
        audit(current_operator(), "skill.rollback", skill_id, version=version)
        return result

    def versions(self, skill_id: str) -> Dict[str, Any]:
        self._locate(skill_id)
        return {"id": skill_id, "items": list_versions(skill_id)}

    def restore(self, skill_id: str, version: int) -> Dict[str, Any]:
        """把历史版本内容恢复为新的草稿版本，不改变当前发布指针。"""
        try:
            result = restore_draft(skill_id, version, actor=current_operator())
        except PublishError as exc:
            raise AdminError(str(exc)) from exc
        audit(current_operator(), "skill.restore_draft", skill_id, version=result["version"])
        return result

    def migrate(self, dry_run: bool = True) -> Dict[str, Any]:
        """为旧 Skill 补齐发布元数据与快照；先预检再写入。"""
        if dry_run:
            return {"dry_run": True, "backup": "", "items": migration_plan()}
        result = migrate_skills(dry_run=False, actor=current_operator())
        audit(current_operator(), "skill.migrate", "", detail=f"{len(result['items'])} 项")
        return result

    # ------------------------------------------------------------------ 附件
    def add_reference(self, skill_id: str, filename: str, content: bytes) -> Dict[str, Any]:
        source = self._package_dir(skill_id)
        name = self._safe_name(filename)
        if Path(name).suffix.lower() not in REFERENCE_SUFFIXES:
            raise AdminValidationError("参考附件只支持 .md / .markdown / .txt")
        if len(content) > MAX_REFERENCE_BYTES:
            raise AdminValidationError("参考附件不能超过 1 MB")
        references = source / "references"
        references.mkdir(parents=True, exist_ok=True)
        target = (references / name).resolve()
        self._require_within(target, references)
        _atomic_write(target, content)
        updated = self._bump_version(skill_id)
        audit(current_operator(), "skill.reference.write", skill_id,
              version=updated.version, detail=name)
        return {"id": skill_id, "version": updated.version, "references": self._reference_names(source)}

    def delete_reference(self, skill_id: str, name: str) -> Dict[str, Any]:
        source = self._package_dir(skill_id)
        references = source / "references"
        target = (references / self._safe_name(name)).resolve()
        self._require_within(target, references)
        if not target.is_file():
            raise AdminNotFound(f"参考附件不存在：{name}")
        target.unlink()
        updated = self._bump_version(skill_id)
        audit(current_operator(), "skill.reference.delete", skill_id,
              version=updated.version, detail=name)
        return {"id": skill_id, "version": updated.version, "references": self._reference_names(source)}

    def upload_template(self, skill_id: str, filename: str, content: bytes) -> Dict[str, Any]:
        source = self._package_dir(skill_id)
        skill = self._load(source, allow_disabled=True)
        if skill.template and skill.template != "assets/template.docx":
            raise AdminConflict("该 Skill 显式配置了 template，请先移除 template 字段")
        if Path(self._safe_name(filename)).suffix.lower() != ".docx":
            raise AdminValidationError("模板只支持 .docx 文件")
        if len(content) > MAX_TEMPLATE_BYTES:
            raise AdminValidationError("模板不能超过 20 MB")
        try:
            from docx import Document
            from io import BytesIO

            Document(BytesIO(content))
        except Exception as exc:
            raise AdminValidationError("模板不是可打开的 .docx 文件") from exc
        assets = source / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        _atomic_write(assets / "template.docx", content)
        updated = self._bump_version(skill_id)
        audit(current_operator(), "skill.template.write", skill_id,
              version=updated.version, detail=filename)
        return {"id": skill_id, "version": updated.version, "template": str(assets / "template.docx")}

    def delete_template(self, skill_id: str) -> Dict[str, Any]:
        source = self._package_dir(skill_id)
        skill = self._load(source, allow_disabled=True)
        if skill.template:
            raise AdminConflict("该 Skill 显式配置了 template，请先移除 template 字段")
        target = source / "assets" / "template.docx"
        if target.is_file():
            target.unlink()
            updated = self._bump_version(skill_id)
            audit(current_operator(), "skill.template.delete", skill_id, version=updated.version)
            return {"id": skill_id, "version": updated.version, "template": ""}
        audit(current_operator(), "skill.template.delete", skill_id)
        return {"id": skill_id, "template": ""}

    # ------------------------------------------------------------------ 内部
    def _locate(self, skill_id: str) -> Path:
        try:
            return locate_skill_source(skill_id)
        except SkillError as exc:
            raise AdminNotFound(str(exc)) from exc

    def _load(self, source: Path, allow_disabled: bool = True) -> DocumentSkill:
        try:
            return load_skill_dir(source, allow_disabled=allow_disabled) if source.is_dir() else load_skill_file(
                source, allow_disabled=allow_disabled
            )
        except SkillError as exc:
            raise AdminValidationError(str(exc)) from exc

    def _read_for_update(self, skill_id: str):
        try:
            return read_skill_for_update(skill_id)
        except SkillError as exc:
            raise AdminNotFound(str(exc)) from exc

    def _bump_version(self, skill_id: str) -> DocumentSkill:
        """资产变更后提升草稿版本，保证发布产生新快照而不是覆盖旧版本。"""
        source, current, snapshot = self._read_for_update(skill_id)
        updated, _ = save_skill_update(source, current, snapshot)
        mark_draft(updated.id, updated.version)
        return updated

    @staticmethod
    def _state_view(skill_id: str) -> Dict[str, Any]:
        """统一发布态读取视图：旧状态自动补齐 runtime_enabled 与生命周期字段。"""
        try:
            raw = read_publish_state(skill_id)
        except PublishError:
            raw = None
        return normalize_state(raw)

    def _package_dir(self, skill_id: str) -> Path:
        source = self._locate(skill_id)
        if not source.is_dir():
            raise AdminConflict("单文件 Skill 不支持附件或模板操作")
        return source

    @staticmethod
    def _clear_publish_artifacts(skill_id: str) -> None:
        clear_publish_artifacts(skill_id)

    @staticmethod
    def _safe_name(filename: str) -> str:
        name = (filename or "").strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            raise AdminValidationError("文件名不能为空或包含路径分隔符")
        return name

    @staticmethod
    def _require_within(target: Path, base: Path) -> None:
        try:
            target.relative_to(base.resolve())
        except ValueError as exc:
            raise AdminValidationError("路径必须位于 Skill 目录内") from exc

    @staticmethod
    def _reference_names(source: Path) -> List[str]:
        references = source / "references"
        if not references.is_dir():
            return []
        return sorted(
            item.name for item in references.iterdir()
            if item.is_file() and item.suffix.lower() in REFERENCE_SUFFIXES
        )

    def _summary(self, skill: DocumentSkill) -> Dict[str, Any]:
        state = self._state_view(skill.id)
        return {
            "id": skill.id,
            "name": skill.name,
            "version": skill.version,
            "enabled": skill.enabled,
            "runtime_enabled": state.get("runtime_enabled", True),
            "lifecycle_status": state.get("lifecycle_status", "active"),
            "description": skill.description,
            "doc_type": skill.task.doc_type,
            "status": state.get("status", "draft"),
            "draft_version": state.get("draft_version", skill.version),
            "published_version": state.get("published_version"),
            "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
        }

    @staticmethod
    def _format_validation(exc: ValidationError) -> str:
        lines = []
        for item in exc.errors()[:5]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "(根)"
            lines.append(f"{location}: {item.get('msg')}")
        return "；".join(lines)
