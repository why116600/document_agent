"""Skill 发布态、不可变快照、发布索引与回滚。

设计原则：
1. ``.state/<id>.json`` 是生产可用性的唯一控制面：发布指针、运行时启停、生命周期；
2. ``.releases/<id>/v<n>/`` 是不可变快照，附带 ``manifest.json`` 校验清单；
3. 运行时只按发布索引读取快照；快照缺失或损坏时显式报错，绝不回退草稿；
4. 草稿 ``enabled`` 只表示「是否允许发布」，不参与运行时筛选；
5. 同版本快照不可覆盖：内容一致时幂等返回，不一致时要求提升版本号。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

from .schema import DocumentSkill
from .store import (
    SKILL_CONFIG_NAMES,
    SkillError,
    _STORE_LOCK,
    _atomic_write,
    list_skills,
    load_skill_dir,
    load_skill_file,
    locate_skill_source,
    resolve_skill_dir,
    set_skill_version,
)

PUBLISH_STATE_VERSION = 2
RELEASE_SCHEMA_VERSION = 1
MAX_BACKUPS_PER_SKILL = 20
CONFIG_SUFFIXES = (".json", ".yaml", ".yml")
MANIFEST_NAME = "manifest.json"
SINGLE_CONFIG_NAME = "skill.json"
LOCK_TIMEOUT_SECONDS = 15.0
LOCK_STALE_SECONDS = 600.0
_IGNORE_PATTERNS = (".history", "__pycache__", ".state", ".releases")
_VERIFIED_RELEASES: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


class PublishError(SkillError):
    """发布/回滚失败；``code`` 供调用方区分故障类型。"""

    def __init__(self, message: str, code: str = "RELEASE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class PublishConflict(PublishError):
    """版本已存在或状态冲突。"""

    def __init__(self, message: str, code: str = "VERSION_CONFLICT") -> None:
        super().__init__(message, code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> Path:
    return resolve_skill_dir() / ".state"


def _release_root() -> Path:
    return resolve_skill_dir() / ".releases"


def publish_state_path(skill_id: str) -> Path:
    return _state_dir() / f"{skill_id}.json"


def release_path(skill_id: str, version: int) -> Path:
    return _release_root() / skill_id / f"v{int(version)}"


# --------------------------------------------------------------------------- 发布态


def normalize_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """兼容旧状态的读取视图：旧字段 ``enabled`` 映射为 ``runtime_enabled``。"""
    data = dict(state or {})
    data.setdefault("schema_version", PUBLISH_STATE_VERSION)
    data.setdefault("lifecycle_status", "active")
    if "runtime_enabled" not in data:
        data["runtime_enabled"] = bool(data.get("enabled", True))
    data.setdefault("published_version", None)
    if not data.get("status"):
        data["status"] = "published" if data.get("published_version") else "draft"
    return data


def read_publish_state(skill_id: str) -> Optional[Dict[str, Any]]:
    """读取发布态；没有状态文件返回 None（表示从未纳入发布流程）。"""
    path = publish_state_path(skill_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishError(f"无法读取发布状态 {path}：{exc}", "STATE_CORRUPT") from exc
    return data if isinstance(data, dict) else None


def write_publish_state(skill_id: str, state: Dict[str, Any]) -> None:
    payload = {"schema_version": PUBLISH_STATE_VERSION, **state}
    path = publish_state_path(skill_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with _STORE_LOCK:
        _atomic_write(path, text.encode("utf-8"))


def mark_draft(skill_id: str, version: int) -> Dict[str, Any]:
    """把 Skill 标记为草稿，保留发布指针与运行时启停开关。"""
    current = normalize_state(read_publish_state(skill_id) or {})
    payload = {
        **current,
        "status": "draft",
        "draft_version": int(version),
    }
    write_publish_state(skill_id, payload)
    return payload


def set_runtime_enabled(skill_id: str, enabled: bool) -> Dict[str, Any]:
    """生产启停开关：只改发布态，不改草稿配置，也不产生新版本。"""
    current = normalize_state(read_publish_state(skill_id) or {})
    payload = {**current, "runtime_enabled": bool(enabled)}
    write_publish_state(skill_id, payload)
    return payload


def mark_deleted(skill_id: str, actor: str = "") -> Dict[str, Any]:
    """软删除墓碑：保留发布记录供审计，索引不再返回该 Skill。"""
    current = normalize_state(read_publish_state(skill_id) or {})
    payload = {
        **current,
        "lifecycle_status": "deleted",
        "deleted_at": _now(),
        "deleted_by": actor,
    }
    write_publish_state(skill_id, payload)
    return payload


def clear_publish_artifacts(skill_id: str) -> None:
    """彻底清理某个 Skill 的发布元数据与快照（仅供重建同名 Skill 使用）。"""
    publish_state_path(skill_id).unlink(missing_ok=True)
    shutil.rmtree(_release_root() / skill_id, ignore_errors=True)


def published_index() -> List[Tuple[str, int]]:
    """枚举可用的发布指针：过滤墓碑、停用与从未发布。"""
    items: List[Tuple[str, int]] = []
    root = _state_dir()
    if not root.is_dir():
        return items
    for path in sorted(root.glob("*.json")):
        try:
            state = normalize_state(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("lifecycle_status") == "deleted":
            continue
        if state.get("runtime_enabled") is False:
            continue
        version = state.get("published_version")
        if not version:
            continue
        items.append((path.stem, int(version)))
    return items


# --------------------------------------------------------------------------- 快照清单


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _file_entries(root: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        if any(part in (".history", "__pycache__") for part in path.relative_to(root).parts):
            continue
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        })
    return entries


def _content_hash(entries: List[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(entries, key=lambda entry: entry["path"]):
        digest.update(item["path"].encode("utf-8"))
        digest.update(item["sha256"].encode("utf-8"))
        digest.update(str(item["size"]).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def read_manifest(release: Path) -> Optional[Dict[str, Any]]:
    path = release / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def verify_release(release: Path) -> Dict[str, Any]:
    """按清单核验快照；通过后按 (目录, 清单 mtime, 文件数) 缓存结果。"""
    manifest = read_manifest(release)
    if manifest is None:
        raise PublishError(f"发布快照缺少 {MANIFEST_NAME}：{release}", "RELEASE_CORRUPT")
    schema_version = int(manifest.get("schema_version") or 0)
    if schema_version > RELEASE_SCHEMA_VERSION:
        raise PublishError(
            f"快照 schema_version={schema_version} 高于当前支持的 {RELEASE_SCHEMA_VERSION}",
            "SCHEMA_UNSUPPORTED",
        )
    try:
        stamp = int((release / MANIFEST_NAME).stat().st_mtime_ns)
    except OSError as exc:
        raise PublishError(f"无法读取发布清单：{exc}", "RELEASE_MISSING") from exc
    key = (str(release), stamp, len(list(release.iterdir())))
    cached = _VERIFIED_RELEASES.get(key)
    if cached is not None:
        return cached
    entries = _file_entries(release)
    if _content_hash(entries) != manifest.get("content_hash"):
        raise PublishError(f"发布快照内容与清单不一致：{release}", "RELEASE_CORRUPT")
    _VERIFIED_RELEASES[key] = manifest
    return manifest


def _release_config(release: Path) -> Path:
    """定位快照内的结构化配置；``manifest.json`` 永不参与候选。"""
    for name in SKILL_CONFIG_NAMES:
        candidate = release / name
        if candidate.is_file():
            return candidate
    legacy = [
        item for item in sorted(release.iterdir())
        if item.is_file() and item.suffix.lower() in CONFIG_SUFFIXES and item.name != MANIFEST_NAME
    ]
    if len(legacy) == 1:
        return legacy[0]
    raise PublishError(f"发布快照缺少可加载的配置：{release}", "RELEASE_CORRUPT")


def load_release(skill_id: str, version: int, verify: bool = True) -> DocumentSkill:
    """按发布指针加载快照；缺失或损坏时抛错，不回退活动目录。"""
    release = release_path(skill_id, version)
    if not release.is_dir():
        raise PublishError(f"Skill「{skill_id}」的发布快照 v{version} 不存在：{release}", "RELEASE_MISSING")
    if verify:
        verify_release(release)
    try:
        if ((release / "SKILL.md").is_file()
                or (release / "references").is_dir()
                or (release / "assets").is_dir()):
            return load_skill_dir(release, allow_disabled=True)
        return load_skill_file(_release_config(release), allow_disabled=True)
    except SkillError as exc:
        raise PublishError(f"发布快照无法加载：{exc}", "RELEASE_CORRUPT") from exc


# --------------------------------------------------------------------------- 发布


@contextmanager
def _release_lock(skill_id: str) -> Iterator[None]:
    """跨进程发布锁：O_EXCL 锁文件，超时或陈旧锁可恢复。"""
    path = _state_dir() / f"{skill_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    handle: Optional[int] = None
    while handle is None:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if stale > LOCK_STALE_SECONDS:
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() > deadline:
                raise PublishError(f"发布锁被占用：{path}", "LOCK_BUSY")
            time.sleep(0.05)
    try:
        os.write(handle, str(os.getpid()).encode("utf-8"))
        yield
    finally:
        os.close(handle)
        path.unlink(missing_ok=True)


def _materialize(source: Path, skill: DocumentSkill, staging: Path) -> None:
    """把活动 Skill 复制/规范化为快照内容；单文件统一写 ``skill.json``。"""
    if source.is_dir():
        shutil.copytree(source, staging, ignore=shutil.ignore_patterns(*_IGNORE_PATTERNS))
        return
    staging.mkdir(parents=True)
    data = skill.model_dump(mode="json", exclude_none=True, exclude={"template_path"})
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    (staging / SINGLE_CONFIG_NAME).write_text(text, encoding="utf-8")


def _build_manifest(skill_id: str, version: int, actor: str, staging: Path) -> Dict[str, Any]:
    entries = _file_entries(staging)
    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "skill_id": skill_id,
        "version": int(version),
        "created_at": _now(),
        "created_by": actor,
        "content_hash": _content_hash(entries),
        "files": entries,
    }


def _write_manifest(release: Path, manifest: Dict[str, Any]) -> None:
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(release / MANIFEST_NAME, text.encode("utf-8"))


def _max_release_version(skill_id: str) -> int:
    root = _release_root() / skill_id
    if not root.is_dir():
        return 0
    versions = [
        int(item.name[1:]) for item in root.iterdir()
        if item.is_dir() and item.name.startswith("v") and item.name[1:].isdigit()
    ]
    return max(versions, default=0)


def publish_skill(skill_id: str, force: bool = False, actor: str = "") -> Dict[str, Any]:
    """把活动草稿发布为不可变快照；同版本内容一致时幂等返回。

    ``force`` 仅用于重建损坏或未完成的快照，不会覆盖内容不同的既有版本。
    """
    source = locate_skill_source(skill_id)
    skill = load_skill_dir(source, allow_disabled=True) if source.is_dir() else load_skill_file(
        source, allow_disabled=True
    )
    if not skill.enabled:
        raise PublishError("草稿处于停用状态，不能发布；请先启用草稿", "DRAFT_DISABLED")

    version = int(skill.version)
    release = release_path(skill.id, version)
    release.parent.mkdir(parents=True, exist_ok=True)

    with _release_lock(skill.id):
        staging = release.parent / f".tmp-{uuid4().hex}"
        try:
            _materialize(source, skill, staging)
            manifest = _build_manifest(skill.id, version, actor, staging)
            _write_manifest(staging, manifest)

            if release.exists():
                existing = read_manifest(release)
                if existing and existing.get("content_hash") == manifest["content_hash"]:
                    return {
                        "id": skill.id,
                        "published_version": version,
                        "release": str(release),
                        "published_at": existing.get("created_at", ""),
                        "content_hash": manifest["content_hash"],
                        "idempotent": True,
                    }
                if existing:
                    raise PublishConflict(
                        f"版本 v{version} 已发布且内容不同；请先修改草稿以提升版本号（当前草稿 v{skill.version}）"
                    )
                # 没有清单说明是上次中断留下的未完成目录，指针不可能指向它
                shutil.rmtree(release, ignore_errors=True)

            staging.rename(release)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

        current = normalize_state(read_publish_state(skill.id) or {})
        if current.get("lifecycle_status") == "deleted":
            raise PublishError("Skill 已被删除，不能发布；请先重建同名 Skill", "SKILL_DELETED")
        state = {
            **current,
            "schema_version": PUBLISH_STATE_VERSION,
            "status": "published",
            "draft_version": int(skill.version),
            "published_version": version,
            "published_at": manifest["created_at"],
            "published_by": actor,
            "release_digest": manifest["content_hash"],
        }
        state.setdefault("runtime_enabled", True)
        write_publish_state(skill.id, state)

    return {
        "id": skill.id,
        "published_version": version,
        "release": str(release),
        "published_at": state["published_at"],
        "content_hash": manifest["content_hash"],
        "idempotent": False,
    }


def list_versions(skill_id: str) -> List[Dict[str, Any]]:
    """列出全部快照，按版本号升序；附带清单摘要与是否可加载。"""
    state = normalize_state(read_publish_state(skill_id) or {})
    published_version = state.get("published_version")
    root = _release_root() / skill_id
    if not root.is_dir():
        return []
    versions: List[Dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_dir() or not path.name.startswith("v"):
            continue
        try:
            version = int(path.name[1:])
        except ValueError:
            continue
        manifest = read_manifest(path) or {}
        versions.append({
            "version": version,
            "path": str(path),
            "published": version == published_version,
            "valid": bool(manifest),
            "content_hash": manifest.get("content_hash", ""),
            "created_at": manifest.get("created_at", ""),
            "published_at": manifest.get("created_at", ""),
        })
    versions.sort(key=lambda item: item["version"])
    return versions


def rollback_skill(skill_id: str, version: int, actor: str = "") -> Dict[str, Any]:
    """只切换发布指针，不改写活动草稿；回滚不改变草稿编辑内容。"""
    release = release_path(skill_id, version)
    manifest = verify_release(release)
    with _release_lock(skill_id):
        current = normalize_state(read_publish_state(skill_id) or {})
        if current.get("lifecycle_status") == "deleted":
            raise PublishError("Skill 已被删除，不能回滚", "SKILL_DELETED")
        state = {
            **current,
            "schema_version": PUBLISH_STATE_VERSION,
            "status": "published",
            "published_version": int(version),
            "rollback_of": current.get("published_version"),
            "published_at": _now(),
            "published_by": actor,
            "release_digest": manifest["content_hash"],
        }
        state.setdefault("runtime_enabled", True)
        write_publish_state(skill_id, state)
    return {
        "id": skill_id,
        "published_version": int(version),
        "release": str(release),
        "content_hash": manifest["content_hash"],
        "rollback_of": state.get("rollback_of"),
    }


def _backup_source(skill_id: str, source: Path) -> Path:
    """把当前活动目录备份到 .history，并清理过期备份。"""
    history = resolve_skill_dir() / ".history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / f"{skill_id}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target / source.name)
    else:
        shutil.copy2(source, target / source.name)
    backups = sorted(history.glob(f"{skill_id}_*"), key=lambda item: item.stat().st_mtime)
    for stale in backups[:-MAX_BACKUPS_PER_SKILL]:
        shutil.rmtree(stale, ignore_errors=True)
    return target


def restore_draft(skill_id: str, version: int, actor: str = "") -> Dict[str, Any]:
    """把历史版本内容拉回活动草稿，并分配大于所有已发布版本的新版本号。"""
    source = locate_skill_source(skill_id)
    snapshot = load_release(skill_id, version)
    backup = _backup_source(skill_id, source)
    target_version = max(int(snapshot.version), _max_release_version(skill_id)) + 1
    if target_version > 99:
        raise PublishError("版本号已达上限 99，请清理历史版本后再恢复", "VERSION_EXHAUSTED")

    if source.is_dir():
        shutil.rmtree(source)
        shutil.copytree(
            release_path(skill_id, version),
            source,
            ignore=shutil.ignore_patterns(MANIFEST_NAME),
        )
    else:
        data = snapshot.model_dump(mode="json", exclude_none=True, exclude={"template_path"})
        text = _dump_config(source, data)
        _atomic_write(source, text.encode("utf-8"))
    restored = set_skill_version(source, target_version)
    mark_draft(skill_id, restored.version)
    return {
        "id": skill_id,
        "version": restored.version,
        "restored_from": int(version),
        "backup": str(backup),
        "restored_by": actor,
    }


def _dump_config(source: Path, data: Dict[str, Any]) -> str:
    if source.suffix.lower() in (".yaml", ".yml"):
        import yaml

        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- 迁移


def migration_plan() -> List[Dict[str, Any]]:
    """预检：列出每个 Skill 的发布指针状态与建议动作。"""
    skills, errors = list_skills()
    items: List[Dict[str, Any]] = []
    for skill in skills:
        try:
            raw = read_publish_state(skill.id)
        except PublishError as exc:
            items.append({"id": skill.id, "action": "repair", "reason": str(exc)})
            continue
        if raw is None:
            items.append({
                "id": skill.id,
                "action": "publish",
                "version": skill.version,
                "reason": "缺少发布元数据",
            })
            continue
        state = normalize_state(raw)
        version = state.get("published_version")
        if not version:
            items.append({"id": skill.id, "action": "skip", "reason": "从未发布"})
            continue
        release = release_path(skill.id, int(version))
        if not release.is_dir():
            items.append({
                "id": skill.id,
                "action": "publish",
                "version": skill.version,
                "reason": f"快照 v{version} 缺失",
            })
            continue
        if read_manifest(release) is None:
            items.append({
                "id": skill.id,
                "action": "publish",
                "version": skill.version,
                "reason": f"快照 v{version} 缺少校验清单",
            })
            continue
        items.append({
            "id": skill.id,
            "action": "ok",
            "version": int(version),
            "reason": "已发布且清单完整",
        })
    return items + [{"id": "", "action": "error", "reason": item} for item in errors]


def _backup_state_root() -> str:
    """迁移前备份现有发布元数据；没有可备份内容时返回空字符串。"""
    root = resolve_skill_dir()
    sources = [root / name for name in (".state", ".releases") if (root / name).is_dir()]
    if not sources:
        return ""
    target = root / f".migration-backup-{datetime.now():%Y%m%d_%H%M%S}"
    target.mkdir(parents=True, exist_ok=True)
    for origin in sources:
        shutil.copytree(origin, target / origin.name)
    return str(target)


def migrate_skills(dry_run: bool = True, actor: str = "migrate") -> Dict[str, Any]:
    """为旧 Skill 生成可加载的快照与发布元数据；先预检再写入。"""
    plan = migration_plan()
    if dry_run:
        return {"dry_run": True, "backup": "", "items": plan}
    backup = _backup_state_root()
    results: List[Dict[str, Any]] = []
    for item in plan:
        if item["action"] != "publish":
            results.append({**item, "result": "skipped"})
            continue
        try:
            published = publish_skill(item["id"], actor=actor)
            results.append({**item, "result": "published", "published_version": published["published_version"]})
        except PublishError as exc:
            results.append({**item, "result": "error", "error": str(exc), "code": exc.code})
    return {"dry_run": False, "backup": backup, "items": results}
