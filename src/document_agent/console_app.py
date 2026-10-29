import os
import sys
import json
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

import document_agent.agent.agent_core as agent_core
from document_agent.memory.neo_graph_db import MemgraphNodeManager
from document_agent.memory.knowledge_graph import KnowledgeGraphMemory
from document_agent.retrieve_tool.remote_kg_tool import RemoteKGTool


def resolve_on_failure(settings, env=None) -> tuple:
    """故障策略：环境变量 ``REMOTE_KG_ON_FAILURE`` > ``setting.json`` > ``degrade``。

    :return: ``(取值, 来源)``；来源仅用于排查，调用方可忽略
    """
    source_env = os.environ if env is None else env
    cfg = dict((settings or {}).get("remote_kg") or {})
    env_value = str(source_env.get("REMOTE_KG_ON_FAILURE") or "").strip().lower()
    cfg_value = str(cfg.get("on_failure") or "").strip().lower()
    if env_value:
        return env_value, "环境变量"
    if cfg_value:
        return cfg_value, "setting.json"
    return "degrade", "代码默认"


if __name__ == "__main__":
    if not os.path.exists("setting.json"):
        print("没找到配置文件")
        sys.exit(-1)
    with open("setting.json","r") as fp:
        setstr=fp.read()
    settings=json.loads(setstr)
    gdb = MemgraphNodeManager(
        uri=settings["gdb_uri"],
        auth=(settings.get("neo4j_user", ""), settings.get("neo4j_password", "")),
        model_name=settings["embedding_model"],
        database=settings.get("neo4j_database", "neo4j")
    )
    try:
        kg = KnowledgeGraphMemory(config_path="setting.json")
    except Exception as exc:
        print(f"知识图谱初始化未就绪（仅使用篇章树运行）：{exc}")
        kg = None
    # 远程知识图谱：可降级依赖，未启用或构造失败都只影响这一路检索，不阻断写作
    remote_kg = None
    try:
        remote_kg = RemoteKGTool.from_settings(settings)
    except Exception as exc:
        print(f"远程知识图谱客户端未启用（仅使用本地检索）：{exc}")
    if remote_kg is None:
        print("远程知识图谱未启用（未配置 remote_kg.enabled=true，或已关闭）")
    else:
        print(
            f"远程知识图谱已启用：{remote_kg.base_url}  "
            f"鉴权：{'开' if remote_kg.auth_enabled else '关'}"
        )
    # 故障策略：环境变量 REMOTE_KG_ON_FAILURE > setting.json > degrade
    remote_kg_cfg = dict(settings.get("remote_kg") or {})
    on_failure, _on_failure_src = resolve_on_failure(settings)
    #改写目标、参考文件、输出路径都由自然语言描述，由intent节点自动识别
    user_input = input("请输入你的需求（自然语言）：")
    core = agent_core.AgentCore()
    core.build_graph(
        gdb,
        kg,
        remote_kg,
        remote_kg_on_failure=on_failure,
        remote_kg_step_timeout=remote_kg_cfg.get("step_timeout"),
        remote_kg_node_timeout=remote_kg_cfg.get("node_timeout"),
    )
    result = core.invoke(user_input, sys.argv[1:])
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
    gdb.close()
    if kg is not None:
        kg.close()
