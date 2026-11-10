"""Skill 管理内部模块，由 console_app 的 skill 子命令调用。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from document_agent.skills.schema import SKILL_FIELD_LABELS, DocumentSkill, explicit_source_restriction
from document_agent.skills.store import (
    SkillError,
    list_skills,
    load_skill,
    load_skill_dir,
    load_skill_file,
    resolve_skill_dir,
    save_skill,
    save_skill_package,
    read_skill_for_update,
    save_skill_update,
)
from document_agent.skills.templates import TEMPLATE_BODY, build_template_skill, draft_skill_prompt


def build_parser(prog: str = "document_agent.console_app skill") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="管理文档 Skill 配置")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出全部 Skill")

    show = sub.add_parser("show", help="查看某个 Skill 的完整配置")
    show.add_argument("ref", help="Skill id、名称、目录或文件路径")
    show.add_argument("--include-disabled", action="store_true", help="允许查看已禁用的 Skill")

    validate = sub.add_parser("validate", help="校验 Skill 配置是否合法")
    validate.add_argument("ref", help="Skill id、名称、目录或文件路径")

    new = sub.add_parser("new", help="生成一份可编辑的 Skill 包（SKILL.md + skill.yaml）")
    new.add_argument("skill_id", help="新 Skill 的 id（小写字母/数字/下划线/短横线）")
    new.add_argument("--name", default=None, help="展示名称，默认与 id 相同")
    new.add_argument("--out", default=None, help="Skill 根目录（包模式）或输出文件（--single 模式）")
    new.add_argument("--single", action="store_true", help="保存为单文件配置而不是 Skill 包")
    new.add_argument("--yaml", action="store_true", help="--single 模式下输出 YAML")
    new.add_argument("--force", action="store_true", help="目标已存在时覆盖")

    draft = sub.add_parser("draft", help="用自然语言描述生成 Skill（需要模型配置）")
    draft.add_argument("description", help="自然语言描述，例如：写一份面向管理层的行业研究报告")
    draft.add_argument("--id", dest="skill_id", default=None, help="指定 Skill id；不填时由模型拟定")
    draft.add_argument("--out", default=None, help="Skill 根目录（包模式）或输出文件（--single 模式）")
    draft.add_argument("--single", action="store_true", help="保存为单文件配置而不是 Skill 包")
    draft.add_argument("--yaml", action="store_true", help="--single 模式下输出 YAML")
    draft.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    draft.add_argument("--sources", nargs="+", choices=["file", "knowledge"],
                       help="明确允许的来源及优先级；模型输出必须符合此约束")

    return parser


def _print_skill(skill: DocumentSkill) -> None:
    print(json.dumps(skill.to_config_dict(), ensure_ascii=False, indent=2))
    print()
    print(f"运行时内容：SKILL.md 指令 {len(skill.instructions)} 字；"
          f"references {len(skill.references)} 个；模板：{skill.template_path or '（无）'}")
    for item in skill.references:
        print(f"  - {item.name}（{len(item.content)} 字）")


def _save(skill: DocumentSkill, args, instructions: Optional[str] = None) -> int:
    if args.single:
        target = Path(args.out) if args.out else resolve_skill_dir() / f"{skill.id}{'.yaml' if args.yaml else '.json'}"
        if target.exists() and not args.force:
            print(f"目标文件已存在：{target}；如需覆盖请加 --force")
            return 1
        try:
            saved = save_skill(skill, target)
        except SkillError as exc:
            print(f"[错误] {exc}")
            return 1
        print(f"已保存：{saved}")
        return 0

    root = Path(args.out) if args.out else None
    try:
        package_dir = save_skill_package(skill, instructions=instructions, root=root, force=args.force)
    except SkillError as exc:
        print(f"[错误] {exc}")
        return 1
    print(f"已创建 Skill 包：{package_dir}")
    print("  - SKILL.md（执行说明，可自由编辑）")
    print("  - skill.yaml（结构化配置，加载时校验）")
    print("  - 可选目录：references/（.md/.txt 会自动注入提示词）、assets/template.docx（文档模板）")
    print("编辑后用 python -m document_agent.console_app skill validate <id> 校验。")
    return 0


def _command_list() -> int:
    skill_dir = resolve_skill_dir()
    skills, errors = list_skills()
    print(f"Skill 目录：{skill_dir}")
    if not skills and not errors:
        print("（目录下没有 Skill，可用 python -m document_agent.console_app skill new <id> 生成模板）")
    for skill in skills:
        print(f"- {skill.id}｜{skill.name}｜{skill.summary()}")
    for error in errors:
        print(f"[跳过] {error}")
    return 0


def _command_show(ref: str, include_disabled: bool) -> int:
    try:
        skill = load_skill(ref, allow_disabled=include_disabled)
    except SkillError as exc:
        print(f"[错误] {exc}")
        return 1
    _print_skill(skill)
    return 0


def _command_validate(ref: str) -> int:
    path = Path(ref)
    try:
        if path.exists():
            skill = load_skill_dir(path, allow_disabled=True) if path.is_dir() else load_skill_file(path, allow_disabled=True)
        else:
            skill = load_skill(ref, allow_disabled=True)
    except SkillError as exc:
        print(f"[校验失败] {exc}")
        return 1
    print(f"[校验通过] {skill.summary()}")
    return 0


def _command_new(args) -> int:
    try:
        skill = build_template_skill(args.skill_id, args.name)
    except Exception as exc:
        print(f"[错误] 模板生成失败：{exc}")
        return 1
    return _save(skill, args, instructions=TEMPLATE_BODY)


def _merge_patch(current, patch):
    """只合并指定键；不允许用 null 删除字段。"""
    result = dict(current)
    for key, value in patch.items():
        if value is None:
            raise ValueError(f"局部修改不允许用 null 删除字段：{key}")
        if isinstance(value, dict):
            result[key] = _merge_patch(result.get(key) if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = value
    return result


# 允许出现在局部补丁里的顶层字段（身份、版本、时间与运行时派生字段统一不收）
PATCHABLE_KEYS = {
    "name", "description", "enabled", "created_by", "task", "structure",
    "research", "evidence", "verification", "instructions", "template",
    "recommendations",
}

# 这些清单一旦由非空变成空，说明配置被整块清空，必须拒绝
_CRITICAL_LISTS = (
    ("structure", "sections"),
    ("research", "questions"),
    ("research", "data_fields"),
    ("verification", "checks"),
)


def _critical_lists_emptied(before, after):
    """找出「原本有内容、改后被清空」的关键清单。"""
    emptied = []
    for block, field in _CRITICAL_LISTS:
        previous = (before.get(block) or {}).get(field) or []
        current = (after.get(block) or {}).get(field) or []
        if previous and not current:
            emptied.append(f"{block}.{field}")
    return emptied


def generate_skill(description, *, skill_id=None, existing=None, llm=None, sources=None):
    """命令行与自然语言入口共用的生成/局部修改及校验，不直接落盘。"""
    from document_agent.agent.llm_client import extract_json_from_text, get_deepseek_llm, llm_invoke
    llm = llm if llm is not None else get_deepseek_llm()
    restriction = explicit_source_restriction(description)
    if sources and restriction and sources != restriction:
        raise SkillError('--sources 与需求中的来源限制冲突')
    required_sources = sources or restriction
    if required_sources:
        print(f"【来源限制】识别到单来源要求：{required_sources}")
    base_prompt = draft_skill_prompt(description, skill_id, existing)
    if required_sources:
        base_prompt += f'\nresearch.source.priority 必须等于 {json.dumps(required_sources)}。'
    prompt, error = base_prompt, ''
    for _ in range(3):
        response = llm_invoke(llm, prompt, retry=3)
        candidates = extract_json_from_text(str(getattr(response, 'content', '') or ''), return_all=True) or []
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if not isinstance(data, dict) or not data:
                    raise ValueError('需要非空JSON对象')
                if existing is not None:
                    unexpected = set(data) - PATCHABLE_KEYS
                    if unexpected:
                        raise ValueError(f'局部修改不允许改这些字段：{sorted(unexpected)}')
                    before = existing.to_config_dict()
                    data = _merge_patch(before, data)
                    skill = DocumentSkill.model_validate(data)
                    emptied = _critical_lists_emptied(before, skill.to_config_dict())
                    if emptied:
                        raise ValueError(f'修改后这些关键清单被清空：{"、".join(emptied)}；请明确说明要改的内容')
                else:
                    skill = DocumentSkill.model_validate(data)
                if skill_id and skill.id != skill_id:
                    raise ValueError(f'id 必须为 {skill_id}')
                if required_sources and skill.research.source.priority != required_sources:
                    raise ValueError(f'research.source.priority 必须等于 {required_sources}')
                if existing is None:
                    if skill.template or skill.template_path or skill.references:
                        raise ValueError('创建规范不创建模板或参考附件，请先将template留空')
                    skill = skill.model_copy(update={'version': 1, 'created_at': None, 'updated_at': None})
                    skill.instructions = '# 执行说明\n\n需求来源：' + description + '\n\n' + skill.instructions
                    skill = DocumentSkill.model_validate(skill.model_dump())
                return skill
            except (ValueError, TypeError) as exc:
                error = str(exc)[:800]
        prompt = base_prompt + '\n【上次输出未通过校验】' + (error or '未找到JSON对象') + '\n请重新返回所需JSON，不要解释。'
    raise SkillError('连续3次未能得到合法规范：' + (error or '模型未返回JSON'))


def manage_skill_request(description, action, *, target=None, llm=None):
    """自然语言管理只操作当前注册目录中的规则，不接受任意文件路径。"""
    if action == 'skill_update':
        if not target:
            raise SkillError('请说明要修改的规范名称或 id')
        source, current, snapshot = read_skill_for_update(target)
        updated = generate_skill(description, skill_id=current.id, existing=current, llm=llm)
        before, after = current.to_config_dict(), updated.to_config_dict()
        changed = sorted(key for key in before.keys() | after.keys() if after.get(key) != before.get(key))
        if not changed:
            raise SkillError('未产生配置变更，请说明具体要修改的内容')
        updated, backup = save_skill_update(source, updated, snapshot)
        summary = '、'.join(SKILL_FIELD_LABELS.get(key, key) for key in changed)
        return {'action': action, 'id': updated.id, 'name': updated.name, 'version': updated.version,
                'path': str(source), 'backup': str(backup), 'changed_fields': changed,
                'message': f'已更新规范「{updated.name}」（{updated.id}），版本v{updated.version}。变更：{summary}。原文件已备份。'}
    if action != 'skill_create':
        raise SkillError('不支持的规范管理操作')
    skill = generate_skill(description, skill_id=target, llm=llm)
    path = save_skill_package(skill, instructions=skill.instructions)
    return {'action': action, 'id': skill.id, 'name': skill.name, 'version': skill.version,
            'path': str(path), 'changed_fields': [], 'message': f'已创建规范「{skill.name}」（{skill.id}），版本v{skill.version}。后续写作可按需求自动选用。'}


def _command_draft(args) -> int:
    try:
        skill = generate_skill(args.description, skill_id=args.skill_id, sources=getattr(args, 'sources', None))
        return _save(skill, args, instructions=skill.instructions)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f'[生成失败] {exc}')
        return 1


def run(argv, *, prog: str = "document_agent.console_app skill") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    if args.command == "list":
        return _command_list()
    if args.command == "show":
        return _command_show(args.ref, args.include_disabled)
    if args.command == "validate":
        return _command_validate(args.ref)
    if args.command == "new":
        return _command_new(args)
    if args.command == "draft":
        return _command_draft(args)
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(run(sys.argv[1:], prog="document_agent.skills.commands"))


