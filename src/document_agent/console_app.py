import argparse
import os
import sys
import json
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

import document_agent.agent.agent_core as agent_core


if __name__ == "__main__":
    gdb = None

    # =========================================================================
    # 【图数据库配置检测与平滑降级说明】
    # 当前为了便于在未配置 Memgraph 或未安装 neo4j 的环境下进行本地文档改写测试，
    # 采用平滑降级方案（若无 setting.json 则 gdb=None，进入纯文档模式）。
    #
    # >>> 后期若正式部署上线、强制要求图数据库服务时，可将本 if-else 降级代码注释掉，
    # >>> 恢复为下方原版的强制退出代码：
    # if not os.path.exists("setting.json"):
    #     print("没找到配置文件 setting.json")
    #     sys.exit(-1)
    # with open("setting.json", "r", encoding="utf-8") as fp:
    #     settings = json.loads(fp.read())
    # from document_agent.memory.neo_graph_db import MemgraphNodeManager
    # gdb = MemgraphNodeManager(uri=settings["gdb_uri"], model_name=settings["embedding_model"])
    # =========================================================================

    if os.path.exists("setting.json"):
        try:
            with open("setting.json", "r", encoding="utf-8") as fp:
                setstr = fp.read()
            settings = json.loads(setstr)
            from document_agent.memory.neo_graph_db import MemgraphNodeManager
            gdb = MemgraphNodeManager(uri=settings["gdb_uri"], model_name=settings["embedding_model"])
            print("[信息] 成功连接 Memgraph 图数据库。")
        except Exception as e:
            print(f"[警告] 初始化图数据库失败（{e}），降级使用纯文档模式运行。")
    else:
        print("[提示] 未检测到 setting.json 配置文件，自动采用纯文档处理模式（无需图数据库）。")

    # 改写目标、参考文件、输出路径都由自然语言描述，由intent节点自动识别
    user_input = input("请输入你的需求（自然语言）：")
    core = agent_core.AgentCore()
    core.build_graph(gdb)
    result = core.invoke(user_input, sys.argv[1:])
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
