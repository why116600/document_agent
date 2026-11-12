"""Skill 管理命令行：结构化操作，不解析自然语言。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from ..store import SkillError
from ..templates import TEMPLATE_BODY, build_template_skill
from .audit import list_audit
from .service import (
    AdminError,
    AdminValidationError,
    SkillAdminService,
)


def build_parser(prog: str = "document_agent skills admin") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="管理文档 Skill（结构化配置）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出全部 Skill")

    show = sub.add_parser("show", help="查看某个 Skill 的完整配置")
    show.add_argument("ref", help="Skill id 或名称")

    validate = sub.add_parser("validate", help="校验 Skill 配置是否合法")
    validate.add_argument("ref", help="Skill id 或名称")

    new = sub.add_parser("new", help="创建一份 Skill 包（SKILL.md + skill.yaml）")
    new.add_argument("skill_id", help="新 Skill 的 id")
    new.add_argument("--name", default=None, help="展示名称，默认与 id 相同")
    new.add_argument("--out", default=None, help="Skill 根目录（包模式）或输出文件（--single 模式）")
    new.add_argument("--single", action="store_true", help="保存为单文件配置而不是 Skill 包")
    new.add_argument("--yaml", action="store_true", help="--single 模式下输出 YAML")
    new.add_argument("--force", action="store_true", help="目标已存在时覆盖")

    update = sub.add_parser("update", help="按 JSON 补丁修改 Skill")
    update.add_argument("skill_id", help="Skill id")
    group = update.add_mutually_exclusive_group(required=True)
    group.add_argument("--patch", help="JSON 字符串，例如 '{\"enabled\": false}'")
    group.add_argument("--file", help="包含 JSON 补丁的文件路径")
    update.add_argument("--version", type=int, default=None, help="期望的当前版本，用于乐观锁")

    delete = sub.add_parser("delete", help="删除 Skill（默认软删除）")
    delete.add_argument("skill_id", help="Skill id")
    delete.add_argument("--purge", action="store_true", help="彻底删除")
    delete.add_argument("--confirm", default="", help="彻底删除时填写 Skill id 以确认")

    for name, help_text in (("enable", "启用 Skill"), ("disable", "停用 Skill")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("skill_id", help="Skill id")

    publish = sub.add_parser("publish", help="发布当前版本")
    publish.add_argument("skill_id", help="Skill id")
    publish.add_argument("--force", action="store_true", help="重建损坏快照；不会覆盖内容不同的既有版本")

    rollback = sub.add_parser("rollback", help="切换发布指针到指定历史版本")
    rollback.add_argument("skill_id", help="Skill id")
    rollback.add_argument("version", type=int, help="目标版本号")

    versions = sub.add_parser("versions", help="列出发布历史")
    versions.add_argument("skill_id", help="Skill id")

    restore = sub.add_parser("restore", help="把历史版本恢复为新的草稿版本")
    restore.add_argument("skill_id", help="Skill id")
    restore.add_argument("version", type=int, help="要恢复的版本号")

    migrate = sub.add_parser("migrate", help="为旧 Skill 补齐发布元数据与快照")
    migrate.add_argument("--apply", action="store_true", help="实际写入；默认只做预检")

    logs = sub.add_parser("audit", help="查看审计记录")
    logs.add_argument("--skill-id", default="", help="只看某个 Skill")
    logs.add_argument("--limit", type=int, default=100, help="最多返回条数")

    draft = sub.add_parser("draft", help="用自然语言创建或修改 Skill")
    draft.add_argument("description", help="自然语言需求或修改要求")
    draft.add_argument("--id", dest="skill_id", default=None, help="创建时的 Skill id；修改时必填")
    draft.add_argument("--update", action="store_true", help="修改已有 Skill")
    draft.add_argument("--publish", action="store_true", help="创建后立即发布")
    draft.add_argument("--version", type=int, default=None, help="修改时的期望版本")
    draft.add_argument("--out", default=None, help="创建时的 Skill 根目录")
    draft.add_argument("--force", action="store_true", help="覆盖已存在的 Skill")
    return parser


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def run(argv, *, prog: str = "document_agent skills admin") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    service = SkillAdminService()

    try:
        if args.command == "list":
            data = service.list(include_disabled=True)
            print(f"Skill 总数：{data['total']}")
            for item in data["items"]:
                runtime = "生产启用" if item.get("runtime_enabled", True) else "生产停用"
                print(
                    f"- {item['id']}｜{item['name']}｜v{item['version']}｜"
                    f"草稿{'可发布' if item['enabled'] else '停用'}｜{runtime}｜{item['status']}"
                )
            return 0
        if args.command == "show":
            _print_json(service.get(args.ref))
            return 0
        if args.command == "validate":
            result = service.validate(args.ref)
            if result["valid"]:
                print(f"[校验通过] {result['summary']}")
                return 0
            print(f"[校验失败] {result['error']}")
            return 1
        if args.command == "new":
            skill = build_template_skill(args.skill_id, args.name)
            result = service.create(
                skill.to_config_dict(),
                instructions=TEMPLATE_BODY,
                publish=True,
                single=args.single,
                as_yaml=args.yaml,
                root=args.out,
                force=args.force,
            )
            print(f"已创建：{result['source']}")
            return 0
        if args.command == "update":
            raw = Path(args.file).read_text(encoding="utf-8-sig") if args.file else args.patch
            patch = json.loads(raw)
            if not isinstance(patch, dict):
                raise AdminValidationError("补丁必须是 JSON 对象")
            result = service.update(args.skill_id, patch, expected_version=args.version)
            print(f"已更新：{result['id']} v{result['version']}（备份：{result['backup']}）")
            return 0
        if args.command == "delete":
            if args.purge and args.confirm != args.skill_id:
                print(f"[错误] 彻底删除需要在 --confirm 中填写 {args.skill_id}")
                return 1
            result = service.delete(args.skill_id, purge=args.purge)
            print(f"已删除：{result['id']}（{result['trash']}）")
            return 0
        if args.command in ("enable", "disable"):
            result = service.set_enabled(args.skill_id, args.command == "enable")
            state = result.get("runtime_enabled", result["enabled"])
            print(f"生产中已{'启用' if state else '停用'}：{result['id']}（草稿内容不变）")
            return 0
        if args.command == "publish":
            result = service.publish(args.skill_id, force=args.force)
            print(f"已发布：{result['id']} v{result['published_version']}")
            return 0
        if args.command == "rollback":
            result = service.rollback(args.skill_id, args.version)
            print(f"已回滚：{result['id']} 到 v{result['published_version']}")
            return 0
        if args.command == "versions":
            data = service.versions(args.skill_id)
            for item in data["items"]:
                mark = "（当前发布）" if item["published"] else ""
                state = "" if item.get("valid") else "（清单缺失/损坏）"
                print(f"- v{item['version']}{mark}{state}")
            return 0
        if args.command == "restore":
            result = service.restore(args.skill_id, args.version)
            print(f"已恢复 v{result['restored_from']} 为草稿 v{result['version']}（备份：{result['backup']}）")
            return 0
        if args.command == "migrate":
            data = service.migrate(dry_run=not args.apply)
            for item in data["items"]:
                detail = item.get("reason") or item.get("error") or ""
                print(f"- {item.get('id') or '(全局)'}：{item['action']}｜{detail}")
            if data["dry_run"]:
                print("预检完成；加 --apply 才会写入（写入前自动备份 .state/.releases）")
            elif data["backup"]:
                print(f"迁移完成；备份目录：{data['backup']}")
            else:
                print("迁移完成；此前没有发布元数据，无需备份")
            return 0
        if args.command == "audit":
            for item in list_audit(args.skill_id, limit=args.limit):
                print(json.dumps(item, ensure_ascii=False))
            return 0
        if args.command == "draft":
            if args.update:
                if not args.skill_id:
                    raise AdminValidationError("修改 Skill 需要 --id")
                result = service.draft_update(
                    args.skill_id,
                    args.description,
                    expected_version=args.version,
                )
                print(f"已更新草稿：{result['id']} v{result['version']}（备份：{result['backup']}）")
            else:
                result = service.draft_create(
                    args.description,
                    skill_id=args.skill_id,
                    publish=args.publish,
                    root=args.out,
                    force=args.force,
                )
                status = (result.get("state") or {}).get("status", "draft")
                print(f"已创建草稿：{result['source']}（状态：{status}）")
            return 0
    except (AdminError, SkillError, json.JSONDecodeError, OSError) as exc:
        print(f"[错误] {exc}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:], prog="document_agent.skills.admin"))
