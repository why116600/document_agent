import os
import sys
import json
import asyncio
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

import document_agent.agent.agent_core as agent_core
from remote_kg_config import find_setting_path, load_remote_kg_settings


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


if __name__ == "__main__":
    setting_path = find_setting_path("setting.json" if os.path.exists("setting.json") else None)
    if not setting_path.is_file():
        print("没找到配置文件")
        sys.exit(-1)
    with open(setting_path, "r", encoding="utf-8") as fp:
        settings = json.loads(fp.read())

    # 知识图谱检索阶段用远程知识图谱还是本地图数据库，由 setting.json 的 remote_kg 段决定
    remote_kg = load_remote_kg_settings(settings=settings, config_path=str(setting_path))
    print(f"【知识图谱】{remote_kg.describe()}")

    gdb = None
    kg = None
    if remote_kg.enabled:
        available, message = probe_remote_kg(remote_kg)
        print(f"【远程知识图谱】{remote_kg.base_url} {message}")
        if not available:
            print("提示：远程知识图谱不可用，检索阶段会直接报错。请确认地址与服务状态后重试。")
    else:
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
    user_input = input("请输入你的需求（自然语言）：")
    core = agent_core.AgentCore()
    core.build_graph(gdb, kg, remote_kg)
    result = core.invoke(user_input, sys.argv[1:])
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
        if result.get("error_code") == "MAIN_ENTRY_MANAGEMENT_FORBIDDEN":
            print("提示：Skill 管理请使用 /admin 或 python -m document_agent.skill_cli skill")
    if gdb is not None:
        gdb.close()
    if kg is not None:
        kg.close()
