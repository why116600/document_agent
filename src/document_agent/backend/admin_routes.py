"""管理入口路由：/admin/api/*，结构化 CRUD，不支持自然语言。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from document_agent.skills.admin.audit import list_audit
from document_agent.skills.admin.service import SkillAdminService

router = APIRouter(prefix="/api", tags=["admin"])
service = SkillAdminService()


class SkillCreateRequest(BaseModel):
    config: Dict[str, Any] = Field(description="DocumentSkill 结构化配置")
    instructions: str = Field(default="", description="SKILL.md 正文")
    publish: bool = Field(default=False, description="创建后是否立即发布")


class SkillPatchRequest(BaseModel):
    version: Optional[int] = Field(default=None, description="期望的当前版本，用于乐观锁")
    patch: Dict[str, Any] = Field(description="仅包含要修改的字段；不允许 null 删除")


class SkillPutRequest(BaseModel):
    version: Optional[int] = Field(default=None, description="期望的当前版本，用于乐观锁")
    config: Dict[str, Any] = Field(description="可变配置字段；身份与版本字段由服务维护")


class RollbackRequest(BaseModel):
    version: int = Field(description="目标历史版本号")


class SkillDraftCreateRequest(BaseModel):
    description: str = Field(description="自然语言需求")
    id: Optional[str] = Field(default=None, description="可选的 Skill id")
    publish: bool = Field(default=False, description="生成后是否立即发布")


class SkillDraftUpdateRequest(BaseModel):
    description: str = Field(description="自然语言修改要求")
    version: Optional[int] = Field(default=None, description="期望的当前版本")


class SkillDraftPreviewRequest(BaseModel):
    description: str = Field(description="自然语言需求")
    id: Optional[str] = Field(default=None, description="创建预览时可选的 Skill id")


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "skills-admin"}


# --------------------------------------------------------------------------- 自然语言管理
@router.post("/skills/draft", status_code=201)
def draft_create(payload: SkillDraftCreateRequest) -> Dict[str, Any]:
    return service.draft_create(
        payload.description,
        skill_id=payload.id,
        publish=payload.publish,
    )


@router.post("/skills/draft/preview")
def draft_create_preview(payload: SkillDraftPreviewRequest) -> Dict[str, Any]:
    return service.draft_preview(payload.description, skill_id=payload.id)


@router.post("/skills/{skill_id}/draft")
def draft_update(skill_id: str, payload: SkillDraftUpdateRequest) -> Dict[str, Any]:
    return service.draft_update(
        skill_id,
        payload.description,
        expected_version=payload.version,
    )


@router.post("/skills/{skill_id}/draft/preview")
def draft_update_preview(skill_id: str, payload: SkillDraftPreviewRequest) -> Dict[str, Any]:
    return service.draft_preview(payload.description, existing_id=skill_id)


@router.get("/skills")
def list_skills(
    include_disabled: bool = True,
    keyword: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    return service.list(
        include_disabled=include_disabled,
        keyword=keyword,
        page=page,
        size=size,
    )


@router.post("/skills", status_code=201)
def create_skill(payload: SkillCreateRequest) -> Dict[str, Any]:
    return service.create(
        payload.config,
        instructions=payload.instructions,
        publish=payload.publish,
    )


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str) -> Dict[str, Any]:
    return service.get(skill_id)


@router.put("/skills/{skill_id}")
def replace_skill(skill_id: str, payload: SkillPutRequest) -> Dict[str, Any]:
    return service.replace(skill_id, payload.config, expected_version=payload.version)


@router.patch("/skills/{skill_id}")
def patch_skill(skill_id: str, payload: SkillPatchRequest) -> Dict[str, Any]:
    return service.update(skill_id, payload.patch, expected_version=payload.version)


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    purge: bool = False,
    confirm: str = Header(default="", alias="X-Confirm-Skill"),
) -> None:
    if purge and confirm != skill_id:
        raise HTTPException(status_code=400, detail={
            "code": "CONFIRM_REQUIRED",
            "message": "彻底删除需要在 X-Confirm-Skill 中填写 Skill id",
        })
    service.delete(skill_id, purge=purge)


@router.post("/skills/{skill_id}/validate")
def validate_skill(skill_id: str) -> Dict[str, Any]:
    return service.validate(skill_id)


@router.post("/skills/{skill_id}/enable")
def enable_skill(skill_id: str) -> Dict[str, Any]:
    return service.set_enabled(skill_id, True)


@router.post("/skills/{skill_id}/disable")
def disable_skill(skill_id: str) -> Dict[str, Any]:
    return service.set_enabled(skill_id, False)


@router.post("/skills/{skill_id}/publish")
def publish_skill(skill_id: str, force: bool = False) -> Dict[str, Any]:
    return service.publish(skill_id, force=force)


@router.post("/skills/{skill_id}/rollback")
def rollback_skill(skill_id: str, payload: RollbackRequest) -> Dict[str, Any]:
    return service.rollback(skill_id, payload.version)


@router.get("/skills/{skill_id}/versions")
def list_versions(skill_id: str) -> Dict[str, Any]:
    return service.versions(skill_id)


@router.post("/skills/{skill_id}/restore")
def restore_draft(skill_id: str, payload: RollbackRequest) -> Dict[str, Any]:
    return service.restore(skill_id, payload.version)


@router.post("/skills/migrate")
def migrate_skills(dry_run: bool = True) -> Dict[str, Any]:
    return service.migrate(dry_run=dry_run)


@router.get("/runtime/diagnostics")
def runtime_diagnostics() -> Dict[str, Any]:
    from document_agent.skills.runtime_api import runtime_diagnostics as collect

    return {"items": collect()}


@router.post("/skills/{skill_id}/references", status_code=201)
async def upload_reference(skill_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    content = await file.read()
    try:
        return await run_in_threadpool(
            service.add_reference, skill_id, file.filename or "", content
        )
    finally:
        await file.close()


@router.delete("/skills/{skill_id}/references/{name:path}")
def delete_reference(skill_id: str, name: str) -> Dict[str, Any]:
    return service.delete_reference(skill_id, name)


@router.post("/skills/{skill_id}/template", status_code=201)
async def upload_template(skill_id: str, file: UploadFile = File(...)) -> Dict[str, Any]:
    content = await file.read()
    try:
        return await run_in_threadpool(
            service.upload_template, skill_id, file.filename or "", content
        )
    finally:
        await file.close()


@router.delete("/skills/{skill_id}/template")
def delete_template(skill_id: str) -> Dict[str, Any]:
    return service.delete_template(skill_id)


@router.get("/audit")
def audit_logs(
    skill_id: str = "",
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    return {"items": list_audit(skill_id, limit=limit)}


__all__ = ["router"]
