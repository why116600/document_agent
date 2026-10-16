import sys
import os
import json
import uuid
from pathlib import Path
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'memory'))

from document_agent.agent.llm_client import get_deepseek_llm
from document_agent.retrieve_tool.summary_dag import get_chunks,build_retrieve_dag
from document_agent.retrieve_tool.extract_document import extract_document_content
from document_agent.memory.neo_graph_db import MemgraphNodeManager
from document_agent.memory.knowledge_graph import KnowledgeGraphMemory

DATA_LABLE="global"
REL_TYPE="ref"

if __name__=="__main__":
    if not os.path.exists("setting.json"):
        print("没找到配置文件")
        sys.exit(-1)
    if len(sys.argv)<=1:
        print("请将要录入知识库的文件路径作为启动参数")
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
    kg=KnowledgeGraphMemory(config_path="setting.json")
    llm=get_deepseek_llm()
    for i in range(1,len(sys.argv)):
        p=Path(sys.argv[i])
        if not p.exists():
            print(f"文件{p}不存在")
            continue
        content=extract_document_content(str(p))
        if content is None:
            continue
        print(f"正在抽取并沉淀文件 {str(p)} 的知识图谱实体与关系...")
        try:
            kg.add_text(content, source=str(p))
        except Exception as exc:
            print(f"知识图谱实体抽取跳过：{exc}")
        print(f"对文件{str(p)}进行切块")
        chunks=get_chunks(llm,content)
        print(f"建立文件{str(p)}的摘要树")
        chunks_list=[unit.complete_txt for unit in chunks]
        dag=build_retrieve_dag(llm,chunks_list)
        # 先将根部录入到图中
        forwards=[]
        for item in dag:
            dbitem={
                "id":str(uuid.uuid4()),
                "content":item.get("content",""),
                "summary":item.get("summary",""),
                "source_file":str(p)
            }
            forwards.append((dbitem["id"],item))
            gdb.insert_node(DATA_LABLE,dbitem,"summary")
        # 将非根部节点录入，并录入对应的边
        while len(forwards)>0:
            next_forwards=[]
            for id,item in forwards:
                backwards=item.get("backward",[])
                if item.get("visited",False):
                    continue
                item["visited"]=True
                for subitem in backwards:
                    sid=str(uuid.uuid4())
                    dbitem={
                        "id":sid,
                        "content":subitem.get("content",""),
                        "summary":subitem.get("summary",""),
                        "source_file":str(p)
                    }
                    next_forwards.append((sid,subitem))
                    gdb.insert_node(DATA_LABLE,dbitem,"summary")
                    gdb.create_edge(DATA_LABLE,{"id":id},DATA_LABLE,{"id":sid},REL_TYPE)
            forwards=next_forwards
        print(f"完成{str(p)}的摘要树录入")
    gdb.close()
    kg.close()