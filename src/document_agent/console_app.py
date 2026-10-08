import os
import sys
import json
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

import document_agent.agent.agent_core as agent_core
from document_agent.memory.neo_graph_db import MemgraphNodeManager


if __name__ == "__main__":
    if not os.path.exists("setting.json"):
        print("没找到配置文件")
        sys.exit(-1)
    with open("setting.json","r") as fp:
        setstr=fp.read()
    settings=json.loads(setstr)
    gdb=MemgraphNodeManager(uri=settings["gdb_uri"],model_name=settings["embedding_model"])
    #改写目标、参考文件、输出路径都由自然语言描述，由intent节点自动识别
    user_input = input("请输入你的需求（自然语言）：")
    core = agent_core.AgentCore()
    core.build_graph(gdb)
    result = core.invoke(user_input, sys.argv[1:])
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
