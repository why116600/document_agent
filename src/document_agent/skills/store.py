"""Skill 的发现、加载、保存与列举。

支持两种组织方式：

1. **Skill 包目录**（推荐）::

       skills/<id>/
       ├── SKILL.md          # 人类可读的名称/用途/执行流程/主要指令（可选 YAML frontmatter）
       ├── skill.yaml        # 机器可校验的结构化配置（也支持 skill.json / skill.yml）
       ├── references/       # 可选：术语表、详细规范、示例（.md/.txt 自动注入提示词）
       └── assets/           # 可选：template.docx 作为新文档的版式基底

2. **单文件配置**：``skills/<id>.json|.yaml``，便于最小化试用。

目录解析顺序：环境变量 ``DOCUMENT_SKILLS_DIR`` → 当前目录 ``skills/`` → ``src/skills/`` → 包内源码旁的 ``src/skills/``。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from .schema import (
    DocumentSkill,
    MAX_INSTRUCTIONS_CHARS,
    MAX_REFERENCE_CHARS,
    MAX_REFERENCE_ITEMS,
    ReferenceDoc,
)

SKILL_FILE_SUFFIXES = (".json", ".yaml", ".yml")
SKILL_CONFIG_NAMES = ("skill.yaml", "skill.yml", "skill.json")
REFERENCE_SUFFIXES = (".md", ".markdown", ".txt")
MAX_REFERENCES_TOTAL_CHARS = 12000
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class SkillError(RuntimeError):
    """Skill 不存在、无法读取、不合法或已禁用。"""


def resolve_skill_dir() -> Path:
    """返回 Skill 根目录；都不存在时返回当前目录下的 ``skills/``，由保存动作按需创建。"""
    env_dir = os.getenv("DOCUMENT_SKILLS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    cwd = Path.cwd()
    candidates = (
        cwd / "skills",
        cwd / "src" / "skills",
        Path(__file__).resolve().parents[2] / "skills",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return (cwd / "skills").resolve()


def _format_validation_error(exc: ValidationError) -> str:
    lines: List[str] = []
    for item in exc.errors()[:5]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "(根)"
        lines.append(f"{location}: {item.get('msg')}")
    return "；".join(lines)


def _load_yaml(text: str, source: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 依赖已声明 pyyaml
        raise SkillError(f"{source} 需要 YAML 解析，但当前环境未安装 pyyaml") from exc
    try:
        return yaml.safe_load(text)
    except Exception as exc:
        raise SkillError(f"{source} 不是合法 YAML：{exc}") from exc


def _read_mapping(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"无法读取 Skill 文件 {path}：{exc}") from exc

    if path.suffix.lower() in (".yaml", ".yml"):
        data = _load_yaml(text, path)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SkillError(f"{path} 不是合法 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise SkillError(f"{path} 的顶层必须是对象（键值对），当前为 {type(data).__name__}")
    return data


def parse_skill_md(text: str, source: Path) -> Tuple[Dict[str, Any], str]:
    """解析 SKILL.md 的可选 YAML frontmatter，返回 (元数据, 正文)。"""
    match = FRONTMATTER_PATTERN.match(text.lstrip("\ufeff"))
    if not match:
        return {}, text.strip()
    meta = _load_yaml(match.group(1), source)
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SkillError(f"{source} 的 frontmatter 必须是键值对")
    return {str(key): value for key, value in meta.items()}, match.group(2).strip()


def _merge_frontmatter(data: Dict[str, Any], meta: Dict[str, Any], directory_name: str) -> Dict[str, Any]:
    """把 SKILL.md 的 frontmatter 合并进配置；冲突的机器字段直接报错，避免两份真相。"""
    merged = dict(data)
    for key in ("id", "name", "version"):
        if key in meta and key in merged and meta[key] != merged[key]:
            raise SkillError(
                f"SKILL.md frontmatter 的 {key}（{meta[key]!r}）与 skill.yaml 的 {key}（{merged[key]!r}）不一致，"
                "请只保留一处定义"
            )
    if not merged.get("id"):
        merged["id"] = meta.get("id") or directory_name
    for key in ("name", "version", "description", "enabled"):
        if key not in merged or merged[key] in (None, ""):
            if key in meta:
                merged[key] = meta[key]
    return merged


def _resolve_within(base: Path, relative: str) -> Path:
    """把相对路径解析到 base 内；越界（如 ../../）直接报错。"""
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise SkillError(f"模板路径必须位于 Skill 目录内：{relative}") from exc
    return candidate


def _load_references(package_dir: Path) -> List[ReferenceDoc]:
    """读取 references/ 下的 .md/.txt，按单文件与总量上限截断。"""
    reference_dir = package_dir / "references"
    if not reference_dir.is_dir():
        return []

    items: List[ReferenceDoc] = []
    total = 0
    for path in sorted(reference_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in REFERENCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        content = text.strip()
        if len(content) > MAX_REFERENCE_CHARS:
            # 预留截断标记的长度，避免截断后反而超过 schema 的上限
            keep = max(MAX_REFERENCE_CHARS - 15, 0)
            content = content[:keep] + "\n…（内容超长，已截断）"
        if total + len(content) > MAX_REFERENCES_TOTAL_CHARS:
            break
        items.append(ReferenceDoc(name=path.relative_to(package_dir).as_posix(), content=content))
        total += len(content)
        if len(items) >= MAX_REFERENCE_ITEMS:
            break
    return items


def _resolve_template(package_dir: Path, configured: str) -> str:
    """确定文档模板路径：显式配置优先，否则自动识别 assets/template.docx。"""
    if configured:
        candidate = _resolve_within(package_dir, configured)
        if candidate.suffix.lower() != ".docx":
            raise SkillError(f"template 必须是 .docx 文件：{configured}")
        if not candidate.is_file():
            raise SkillError(f"template 指向的文件不存在：{configured}")
        return str(candidate)

    default = package_dir / "assets" / "template.docx"
    return str(default.resolve()) if default.is_file() else ""


def load_skill_file(path: str | Path, allow_disabled: bool = False) -> DocumentSkill:
    """按路径加载并校验单个配置文件（不读取 SKILL.md/references/assets）。"""
    path_obj = Path(path).expanduser()
    if not path_obj.exists():
        raise SkillError(f"Skill 文件不存在：{path_obj}")
    if not path_obj.is_file():
        raise SkillError(f"Skill 路径不是文件：{path_obj}")
    data = _read_mapping(path_obj)
    try:
        skill = DocumentSkill.model_validate(data)
    except ValidationError as exc:
        raise SkillError(f"{path_obj} 校验失败：{_format_validation_error(exc)}") from exc
    if not skill.enabled and not allow_disabled:
        raise SkillError(f"Skill「{skill.name}」（{skill.id}）已禁用，请先在配置中把 enabled 设为 true")
    return skill


def load_skill_dir(path: str | Path, allow_disabled: bool = False) -> DocumentSkill:
    """加载 Skill 包目录：配置 + SKILL.md + references/ + assets/。"""
    package_dir = Path(path).expanduser()
    if not package_dir.is_dir():
        raise SkillError(f"Skill 目录不存在：{package_dir}")

    config_candidates = [package_dir / name for name in SKILL_CONFIG_NAMES if (package_dir / name).is_file()]
    if not config_candidates:
        raise SkillError(
            f"{package_dir} 缺少结构化配置（期望 {'/'.join(SKILL_CONFIG_NAMES)} 之一）"
        )
    if len(config_candidates) > 1:
        raise SkillError(
            f"{package_dir} 同时存在多个配置文件："
            + "、".join(item.name for item in config_candidates)
            + "，请只保留一个"
        )

    data = _read_mapping(config_candidates[0])
    instructions = ""
    md_path = package_dir / "SKILL.md"
    if md_path.is_file():
        try:
            meta, instructions = parse_skill_md(md_path.read_text(encoding="utf-8"), md_path)
        except OSError as exc:
            raise SkillError(f"无法读取 {md_path}：{exc}") from exc
        data = _merge_frontmatter(data, meta, package_dir.name)

    data["instructions"] = instructions or data.get("instructions", "")
    if data["instructions"] and len(data["instructions"]) > MAX_INSTRUCTIONS_CHARS:
        raise SkillError(
            f"{package_dir} 的 SKILL.md 正文超过 {MAX_INSTRUCTIONS_CHARS} 字，请精简后再加载"
        )

    data["references"] = [item.model_dump() for item in _load_references(package_dir)]
    data["template_path"] = _resolve_template(package_dir, str(data.get("template") or ""))

    try:
        skill = DocumentSkill.model_validate(data)
    except ValidationError as exc:
        raise SkillError(f"{package_dir} 校验失败：{_format_validation_error(exc)}") from exc

    if not skill.enabled and not allow_disabled:
        raise SkillError(f"Skill「{skill.name}」（{skill.id}）已禁用，请先在配置中把 enabled 设为 true")
    return skill


def _iter_skill_sources(skill_dir: Path) -> List[Path]:
    """列出根目录下的单文件 Skill 与包含配置文件的子目录。"""
    if not skill_dir.is_dir():
        return []

    sources: List[Path] = []
    for path in sorted(skill_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in SKILL_FILE_SUFFIXES:
            sources.append(path)
        elif path.is_dir() and any((path / name).is_file() for name in SKILL_CONFIG_NAMES):
            sources.append(path)
    return sources


def _load_source(path: Path, allow_disabled: bool = True) -> DocumentSkill:
    if path.is_dir():
        return load_skill_dir(path, allow_disabled=allow_disabled)
    return load_skill_file(path, allow_disabled=allow_disabled)


def load_skill(ref: str, allow_disabled: bool = False) -> DocumentSkill:
    """按「目录/文件路径 / Skill id / 展示名称」加载 Skill。

    id 精确匹配优先；名称支持包含式模糊匹配，命中多个时要求用户指明具体 id。
    """
    ref = (ref or "").strip()
    if not ref:
        raise SkillError("未提供 Skill 名称或 id")

    ref_path = Path(ref).expanduser()
    if ref_path.exists():
        if ref_path.is_dir():
            return load_skill_dir(ref_path, allow_disabled=allow_disabled)
        return load_skill_file(ref_path, allow_disabled=allow_disabled)

    skill_dir = resolve_skill_dir()
    sources = _iter_skill_sources(skill_dir)
    if not sources:
        raise SkillError(
            f"未找到 Skill「{ref}」：{skill_dir} 下没有任何 Skill 文件或 Skill 目录"
        )

    errors: List[str] = []
    exact: List[DocumentSkill] = []
    fuzzy: List[DocumentSkill] = []
    disabled: List[DocumentSkill] = []
    for path in sources:
        try:
            skill = _load_source(path, allow_disabled=True)
        except SkillError as exc:
            errors.append(str(exc))
            continue
        enabled_or_allowed = skill.enabled or allow_disabled
        if skill.id.lower() == ref.lower() or skill.name == ref:
            (exact if enabled_or_allowed else disabled).append(skill)
        elif ref in skill.name or skill.name in ref:
            (fuzzy if enabled_or_allowed else disabled).append(skill)

    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SkillError(f"有多个 Skill 与「{ref}」精确匹配：" + "、".join(s.id for s in exact))

    if len(fuzzy) == 1:
        print(f"【Skill】按名称模糊匹配到：{fuzzy[0].name}（{fuzzy[0].id}）")
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise SkillError(
            f"「{ref}」匹配到多个 Skill：" + "、".join(f"{s.name}({s.id})" for s in fuzzy)
            + "，请改用具体 id"
        )

    if disabled and not allow_disabled:
        target = disabled[0]
        raise SkillError(
            f"Skill「{target.name}」（{target.id}）已禁用，请先在配置中把 enabled 设为 true"
        )

    detail = "；".join(errors[:3])
    available = "、".join(path.stem if path.is_file() else path.name for path in sources)
    raise SkillError(
        f"未找到 Skill「{ref}」。目录：{skill_dir}；可用：{available}"
        + (f"；加载错误：{detail}" if detail else "")
    )


def list_skills() -> Tuple[List[DocumentSkill], List[str]]:
    """加载目录下全部 Skill（含禁用项），返回 (列表, 错误说明列表)。"""
    skills: List[DocumentSkill] = []
    errors: List[str] = []
    seen: Dict[str, Path] = {}
    for path in _iter_skill_sources(resolve_skill_dir()):
        try:
            skill = _load_source(path, allow_disabled=True)
        except SkillError as exc:
            errors.append(str(exc))
            continue
        if skill.id in seen:
            errors.append(
                f"重复的 Skill id「{skill.id}」：{seen[skill.id]} 与 {path}，已保留前者"
            )
            continue
        seen[skill.id] = path
        skills.append(skill)
    skills.sort(key=lambda item: item.id)
    return skills, errors


def list_skill_summaries() -> List[Tuple[str, str, str]]:
    """给意图识别提供已启用的 Skill 清单：返回 (id, 名称, 触发词)。"""
    skills, _ = list_skills()
    return [
        (skill.id, skill.name, "、".join(skill.task.triggers))
        for skill in skills
        if skill.enabled
    ]


def _touch_timestamps(skill: DocumentSkill) -> DocumentSkill:
    now = datetime.now()
    created = skill.created_at or now
    return skill.model_copy(update={"created_at": created, "updated_at": now})


def save_skill(skill: DocumentSkill, out_path: Optional[str | Path] = None, as_yaml: bool = False) -> Path:
    """保存为单文件 Skill；未给出路径时写入 ``<skill_dir>/<id>.json``（YAML 用 ``.yaml``）。"""
    if out_path:
        path = Path(out_path).expanduser()
    else:
        path = resolve_skill_dir() / f"{skill.id}{'.yaml' if as_yaml else '.json'}"

    path.parent.mkdir(parents=True, exist_ok=True)
    skill = _touch_timestamps(skill)
    data = skill.to_config_dict()

    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise SkillError("保存 YAML 需要安装 pyyaml，或改用 .json 后缀") from exc
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2)

    path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    return path


def save_skill_package(
    skill: DocumentSkill,
    instructions: Optional[str] = None,
    root: Optional[str | Path] = None,
    force: bool = False,
) -> Path:
    """保存为 Skill 包目录：``skill.yaml`` + ``SKILL.md``。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SkillError("保存 Skill 包需要安装 pyyaml") from exc

    base_dir = Path(root).expanduser() if root else resolve_skill_dir()
    package_dir = base_dir / skill.id
    if package_dir.exists() and not force:
        raise SkillError(f"Skill 目录已存在：{package_dir}；如需覆盖请加 --force")

    package_dir.mkdir(parents=True, exist_ok=True)
    skill = _touch_timestamps(skill)

    config = skill.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"instructions", "references", "template_path"},
    )
    (package_dir / "skill.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    body = instructions if instructions is not None else skill.instructions
    if not body:
        body = (
            "# 执行说明\n\n"
            "在此描述本 Skill 的执行流程与主要指令；正文会被注入到写作与改写提示词中。\n"
        )
    frontmatter = {
        "id": skill.id,
        "name": skill.name,
        "version": skill.version,
        "description": skill.description,
    }
    front_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    (package_dir / "SKILL.md").write_text(
        f"---\n{front_text}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )
    return package_dir
