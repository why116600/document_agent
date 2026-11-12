"""文档 Skill 的独立命令行入口。

设计边界：
- 本模块只负责「用 Skill 写文档」；
- Skill 管理转发到 ``document_agent.skills.admin``，仅本机；支持结构化命令和自然语言创建/修改；
- ``console_app.py`` 保持原有自然语言交互与退出码。

命令行用法::

    # 用 Skill 写文档（可显式指定、非交互、导出报告、验收门禁）
    python -m document_agent.skill_cli --skill project_weekly --request "写本周周报" D:/docs/资料.txt
    python -m document_agent.skill_cli --skill D:/tmp/临时规范.yaml --request-file req.txt --output out.docx
    python -m document_agent.skill_cli --list-skills

    # 管理 Skill（转发到管理 CLI）
    python -m document_agent.skill_cli skill list
    python -m document_agent.skill_cli skill new my_standard --name 文档规范
    python -m document_agent.skill_cli skill draft "创建项目周报规范" --id project_weekly
    python -m document_agent.skill_cli skill draft "把风格改简洁" --id project_weekly --update
    python -m document_agent.skill_cli skill publish project_weekly

也可直接用 ``python -m document_agent.skills.admin <子命令>`` 管理规范。
自然语言管理只在管理入口提供，主入口仍然只读。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import asyncio
from datetime import datetime, timezone
from pathlib import Path

# 与 console_app.py 一致：agent / retrieve_tool 目录里的散装模块依赖 sys.path 注入。
_PACKAGE_DIR = Path(__file__).resolve().parent
for _sub in ("agent", "retrieve_tool"):
    _path = str(_PACKAGE_DIR / _sub)
    if _path not in sys.path:
        sys.path.append(_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m document_agent.skill_cli",
        description="用 Skill 写文档 / 管理文档 Skill（独立命令行入口，不改动 console_app）",
        epilog="管理规范：python -m document_agent.skill_cli skill {list,show,validate,new,draft}；"
               "写文档：python -m document_agent.skill_cli --skill <id> --request \"需求\" [文件...]",
    )
    parser.add_argument("files", nargs="*", help="参考文件或待改写文档")
    parser.add_argument("-s", "--skill", default=None,
                        help="指定 Skill id、名称、目录或配置文件路径（可用未安装的临时配置）")
    parser.add_argument("--list-skills", action="store_true", help="列出 Skill，不连接数据库或模型")
    request = parser.add_mutually_exclusive_group()
    request.add_argument("--request", help="直接传入需求，省去交互输入")
    request.add_argument("--request-file", help="从 UTF-8 文本文件读取需求")
    parser.add_argument("--output", help="明确指定输出文档路径")
    parser.add_argument("--report-json", help="保存结构化生成/检索/验收结果及所用 Skill 快照")
    parser.add_argument("--strict-verification", action="store_true",
                        help="生成成功但验收未通过/未完成时退出码为 2")
    return parser


def write_result_report(path, result):
    if not path:
        return
    report = {key: result.get(key) for key in (
        "state", "error", "save_path", "skill_id", "skill", "retrieval_report",
        "verification_report", "citations", "unmet_requirements", "generation_report",
        "result_type", "skill_operation", "message",
    )}
    report["report_version"] = 1
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    report["runtime"] = {
        "model_name": os.getenv("MODEL_NAME") or "deepseek-chat",
        "python_version": sys.version.split()[0],
    }
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结构化结果：{target}")


def probe_remote_kg(settings):
    """启动前自检远程知识图谱；与 develop 版 console_app 的实现一致。"""
    from document_agent.retrieve_tool.remote_kg_tool import RemoteKGTool

    async def _probe():
        tool = RemoteKGTool(settings.base_url, **settings.tool_kwargs())
        try:
            return await tool.get_status(probe_llm=False)
        finally:
            await tool.aclose()

    try:
        status = asyncio.run(_probe())
    except Exception as exc:
        return False, f"连接失败：{exc}"
    components = status.get("components") or {}
    graph = components.get("graph") or {}
    if not status.get("ok"):
        failures = [
            f"{name}: {item.get('error') or '异常'}"
            for name, item in components.items()
            if isinstance(item, dict) and not item.get("ok")
        ]
        return False, "服务未就绪：" + ("；".join(failures) or "未知原因")
    return True, (
        f"连接正常（版本 {status.get('version')}；文档 {graph.get('documents')}、"
        f"分块 {graph.get('chunks')}、实体 {graph.get('entities')}、关系 {graph.get('relations')}）"
    )


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # 管理规范：离线路径，不加载 agent_core / 模型 / 数据库
    if arguments and arguments[0] == "skill":
        from document_agent.skills.admin.cli import run as run_skill_command
        return run_skill_command(arguments[1:], prog="document_agent.skill_cli skill")

    args = build_parser().parse_args(arguments)
    if args.list_skills:
        from document_agent.skills.runtime_api import list_runtime_summaries, runtime_diagnostics
        from document_agent.skills.store import resolve_skill_dir
        summaries = list_runtime_summaries()
        print(f"Skill 目录：{resolve_skill_dir()}")
        if not summaries:
            print("（没有已发布且生产启用的 Skill）")
            for item in runtime_diagnostics():
                print(f"  ! {item['skill_id']}：{item['code']}｜{item['detail']}")
        for item in summaries:
            print(f"- {item['id']}｜{item['name']}｜适用：{item['doc_type']}｜{item['description']}")
        return 0

    # 写文档路径：此处才导入编排核心，避免 list/validate 等管理命令加载重依赖
    import document_agent.agent.agent_core as agent_core
    from remote_kg_config import RemoteKGSettings, find_setting_path, load_remote_kg_settings
    from document_agent.skills.schema import coerce_skill

    try:
        user_input = (Path(args.request_file).read_text(encoding="utf-8-sig") if args.request_file
                      else args.request if args.request is not None else input("请输入你的需求（自然语言）："))
        if not user_input.strip():
            raise ValueError("需求不能为空")
        core = agent_core.AgentCore()
        prepared = core.prepare_task(user_input, args.files, skill_id=args.skill,
                                     save_path=str(Path(args.output).resolve()) if args.output else '')
        if prepared.get('state') == 'error':
            print(f"[需求解析错误] {prepared.get('error')}")
            write_result_report(args.report_json, prepared)
            return 1
        selected_skill = coerce_skill(prepared.get('skill'))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[错误] {exc}")
        write_result_report(args.report_json, {'state': 'error', 'error': str(exc)})
        return 1

    file_only = selected_skill is not None and selected_skill.research.source.priority == ["file"]
    gdb = None
    kg = None
    if file_only:
        remote_kg = RemoteKGSettings(enabled=False)
        print("【检索来源】Skill 仅允许文件，跳过数据库、向量模型与远程服务初始化")
    else:
        setting_path = find_setting_path("setting.json" if os.path.exists("setting.json") else None)
        if not setting_path.is_file():
            print("没找到配置文件；需要知识库的任务请配置 src/setting.json")
            return 1
        with open(setting_path, "r", encoding="utf-8") as fp:
            settings = json.load(fp)
        remote_kg = load_remote_kg_settings(settings=settings, config_path=str(setting_path))
        print(f"【知识图谱】{remote_kg.describe()}")

    if not file_only and remote_kg.enabled:
        available, message = probe_remote_kg(remote_kg)
        print(f"【远程知识图谱】{remote_kg.base_url} {message}")
        if not available:
            print("提示：远程知识图谱不可用，检索阶段会直接报错。请确认地址与服务状态后重试。")
    elif not file_only:
        from document_agent.memory.neo_graph_db import MemgraphNodeManager
        from document_agent.memory.knowledge_graph import KnowledgeGraphMemory

        gdb = MemgraphNodeManager(
            uri=settings["gdb_uri"],
            auth=(settings.get("neo4j_user", ""), settings.get("neo4j_password", "")),
            model_name=settings["embedding_model"],
            database=settings.get("neo4j_database", "neo4j")
        )
        try:
            kg = KnowledgeGraphMemory(config_path=str(setting_path))
        except Exception as exc:
            print(f"知识图谱初始化未就绪（仅使用篇章树运行）：{exc}")
            kg = None

    try:
        core.build_graph(gdb, kg, remote_kg, knowledge_required=not file_only)
        result = core.invoke(user_input, args.files, skill_id=args.skill, prepared_state=prepared,
                             save_path=str(Path(args.output).resolve()) if args.output else "")
    except Exception as exc:
        result = {"state": "error", "error": str(exc)}
    finally:
        if gdb is not None:
            gdb.close()
        if kg is not None:
            kg.close()

    write_result_report(args.report_json, result)
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
        report = result.get("verification_report") or {}
        if report:
            print("验收结果：", report.get("summary") or "")
        citations = result.get("citations") or []
        if citations:
            print(f"可追溯来源：{len(citations)} 条")
        unmet = result.get("unmet_requirements") or []
        if unmet:
            print("未满足的要求：")
            for item in unmet:
                print("  -", item)
        retrieval = result.get("retrieval_report") or {}
        missing = retrieval.get("missing") or []
        if missing:
            print("检索未覆盖的问题：", "、".join(missing))
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
    if result.get("state") != "succeeded":
        return 1
    if args.strict_verification and not (result.get("verification_report") or {}).get("passed"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
