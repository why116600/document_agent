import os
import sys
import json
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

import document_agent.agent.agent_core as agent_core
from document_agent.memory.neo_graph_db import MemgraphNodeManager
from document_agent.memory.knowledge_graph import KnowledgeGraphMemory


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
    #改写目标、参考文件、输出路径都由自然语言描述，由intent节点自动识别
    user_input = input("请输入你的需求（自然语言）：")
    core = agent_core.AgentCore()
    core.build_graph(gdb, kg)
    result = core.invoke(user_input, sys.argv[1:])
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
    gdb.close()
    if kg is not None:
        kg.close()
