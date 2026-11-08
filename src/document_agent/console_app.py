import argparse
import os
import sys
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

from remote_kg_config import RemoteKGSettings, find_setting_path, load_remote_kg_settings
from document_agent.skills.store import SkillError, list_skills, load_skill, resolve_skill_dir
from document_agent.skills.match import match_skill_for_text


def build_parser():
    parser = argparse.ArgumentParser(
        description="文档写作与改写，可选使用用户定义的 Skill。",
        epilog="管理 Skill：python -m document_agent.console_app skill {list,show,validate,new,draft}；使用 skill --help 查看帮助。",
    )
    parser.add_argument("files", nargs="*", help="参考文件或待改写文档")
    parser.add_argument("-s", "--skill", default=None, help="Skill id、名称、目录或文件路径")
    parser.add_argument("--list-skills", action="store_true", help="列出 Skill，不连接数据库或模型")
    request = parser.add_mutually_exclusive_group()
    request.add_argument("--request", help="直接传入需求，省去交互输入")
    request.add_argument("--request-file", help="从 UTF-8 文本文件读取需求")
    parser.add_argument("--output", help="明确指定输出文档路径")
    parser.add_argument("--report-json", help="保存结构化生成/检索/验收结果及所用 Skill 快照")
    parser.add_argument("--strict-verification", action="store_true", help="生成成功但验收未通过/未完成时退出码为2")
    return parser


def write_result_report(path, result):
    if not path:
        return
    report = {key: result.get(key) for key in (
        "state", "error", "save_path", "skill_id", "skill", "retrieval_report",
        "verification_report", "citations", "unmet_requirements", "generation_report",
    )}
    report["report_version"] = 1
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    report["runtime"] = {"model_name": os.getenv("MODEL_NAME") or "deepseek-flash", "python_version": sys.version.split()[0]}
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结构化结果：{target}")


def probe_remote_kg(settings):
    """启动前自检远程知识图谱：返回 (是否可用, 描述文本)。"""
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


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "skill":
        from document_agent.skills.commands import run as run_skill_command
        return run_skill_command(arguments[1:])
    args = build_parser().parse_args(arguments)
    if args.list_skills:
        skills, errors = list_skills()
        print(f"Skill 目录：{resolve_skill_dir()}")
        for skill in skills:
            print(f"- {skill.id}｜{skill.name}｜{skill.summary()}")
        for error in errors:
            print(f"[跳过] {error}")
        return 0
    try:
        user_input = (Path(args.request_file).read_text(encoding="utf-8-sig") if args.request_file
                      else args.request if args.request is not None else input("请输入你的需求（自然语言）："))
        if not user_input.strip():
            raise ValueError("需求不能为空")
        selected_skill = load_skill(args.skill) if args.skill else match_skill_for_text(user_input)[0]
    except (OSError, ValueError, SkillError) as exc:
        print(f"[错误] {exc}")
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
        # 本地模式才需要 Memgraph 篇章树与本地知识图谱记忆
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

    #改写目标、参考文件、输出路径都由自然语言描述，由intent节点自动识别
    import document_agent.agent.agent_core as agent_core
    try:
        core = agent_core.AgentCore()
        core.build_graph(gdb, kg, remote_kg, knowledge_required=not file_only)
        result = core.invoke(user_input, args.files, skill_id=args.skill or (selected_skill.id if selected_skill else None),
                             save_path=str(Path(args.output).resolve()) if args.output else "")
    except Exception as exc:
        result = {"state": "error", "error": str(exc)}
    finally:
        if gdb is not None:
            gdb.close()
        if kg is not None:
            kg.close()
    write_result_report(args.report_json, result)
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
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
