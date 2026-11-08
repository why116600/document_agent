"""Skill 管理内部模块，由 console_app 的 skill 子命令调用。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from document_agent.skills.schema import DocumentSkill, explicit_source_restriction
from document_agent.skills.store import (
    SkillError,
    list_skills,
    load_skill,
    load_skill_dir,
    load_skill_file,
    resolve_skill_dir,
    save_skill,
    save_skill_package,
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


def _command_draft(args) -> int:
    try:
        from document_agent.agent.llm_client import extract_json_from_text, get_deepseek_llm, llm_invoke
    except Exception as exc:
        print(f"[错误] 无法加载模型客户端：{exc}")
        return 1

    try:
        llm = get_deepseek_llm()
    except Exception as exc:
        print(f"[错误] 模型未配置：{exc}；请先设置 MODEL_API_KEY 等环境变量")
        return 1

    base_prompt = draft_skill_prompt(args.description, args.skill_id)
    try:
        restriction = explicit_source_restriction(args.description)
    except ValueError as exc:
        print(f"[错误] {exc}")
        return 1
    sources = getattr(args, "sources", None)
    if sources and restriction and sources != restriction:
        print("[错误] --sources 与需求中的单来源限制冲突")
        return 1
    required_sources = sources or restriction
    if required_sources:
        base_prompt += f"\n【必须遵守】research.source.priority 必须等于 {json.dumps(required_sources)}。\n"
    prompt = base_prompt
    last_error = ""
    for _ in range(3):
        result = llm_invoke(llm, prompt)
        text = str(getattr(result, "content", "") or "")
        candidates = extract_json_from_text(text, return_all=True) or []
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                skill = DocumentSkill.model_validate(data)
                if args.skill_id and skill.id != args.skill_id:
                    raise ValueError(f"id 必须为 {args.skill_id}")
                if required_sources and skill.research.source.priority != required_sources:
                    raise ValueError(f"research.source.priority 必须等于 {required_sources}，不得添加其他来源")
            except Exception as exc:
                last_error = str(exc)[:500]
                continue
            instructions = (
                "# 执行说明\n\n"
                f"需求来源：{args.description}\n\n"
                + (skill.instructions or "生成的规范见 skill.yaml；如需补充执行流程与主要指令，请直接编辑本文件。\n")
            )
            return _save(skill, args, instructions=instructions)
        prompt = (
            base_prompt
            + f"\n\n【上一次输出不合法】{last_error or '未找到可解析的 JSON 对象'}\n"
            "请严格按字段约束重新输出一个完整 JSON 对象，不要输出任何解释。"
        )

    print(f"[生成失败] 连续 3 次未能得到合法的 Skill JSON：{last_error}")
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


