
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path
import traceback

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.memory.neo_graph_db import MemgraphNodeManager

class GraphRetrieveDecision(BaseModel):
    enough : bool = Field(default=True,description="当前信息是否已经满足检索目标的要求")
    to_expand : List[str] = Field(default=[],description="需要进一步展开检索的信息")

def create_gdb_retrieve_node(llm,gdb : MemgraphNodeManager):
    def gdb_retrieve(state : AgentState) -> AgentState:
        params=state["retrieve_params"]
        retrieve_target=state["retrieve_target"]
        retrieved_items=state["retrieved_content"]
        expandable_nodes={}#可以展开的节点id到retrieved_items对应项的下标以及节点内容的映射
        data_label=state["gdb_label"]
        data_node_backward={}
        if params is None:
            return {**state,"state":"error","error":"图数据库检索缺少参数"}
        print("="*20,"开始进行图数据库的检索，检索目标：",retrieve_target,"="*20)
        print("检索参数：",params)
        res=gdb.search_by_vector(data_label,retrieve_target)
        nroot=len(res)
        if nroot<=0:
            tool_response=f"图数据库无法找到检索关键内容：{retrieve_target}"
            retrieved_items.append(tool_response)
            return {**state,"retrieved_content":retrieved_items}
        for item in res:
            if not "id" in item.keys() or not "content" in item.keys():
                continue
            nid=item["id"]
            backwards=gdb.get_neighbors(data_label,{"id":nid},direction="out")
            data_node_backward[nid]=backwards
            if len(backwards)>0:
                # print(f"可展开的节点[{nid}]内容：",item["content"])
                expandable_nodes[nid]=(len(retrieved_items),item["summary"])
                retrieved_items.append(f"可展开的图数据库节点{nid}的内容：{item["summary"]}")
            else:
                retrieved_items.append(f"不可展开的图数据库节点{nid}的内容：{item["content"]}")
        # 开始大模型摘要dag检索
        for _ in range(10*nroot):
            retrieved_str="\n".join(retrieved_items)
            think_prompt=(
                "你是图数据库检索工具，根据检索的目标判定当前已知信息是否满足检索目标\n"
                "如果不满足则要确定哪些图数据库的节点需要进一步展开检索\n"
                f"检索目标：{retrieve_target}\n"
                f"已知信息：\n{retrieved_str}\n"
                "请输出检索的思路"
            )
            res=llm_invoke(llm,think_prompt)
            content=getattr(res,"content","")
            if len(content)<=0:
                continue
            # print(f"图检索思路：{content}")
            prompt=(
                "你是图数据库检索工具，根据检索的目标和思路判定当前已知信息是否满足检索目标\n"
                "如果不满足则要确定哪些图数据库的节点需要进一步展开检索\n"
                f"检索目标：{retrieve_target}\n"
                f"检索思路：{content}"
                f"已知信息：\n{retrieved_str}\n"
            )
            res=llm_model_invoke(llm,prompt,GraphRetrieveDecision)
            if res is None:
                continue
            if res.enough:
                print("\t检索内容已足够")
                break
            if res.to_expand is None:
                continue
            print("\t需要展开检索的节点：",res.to_expand)
            for nid in res.to_expand:
                if not nid in expandable_nodes:
                    continue
                index,content=expandable_nodes[nid]
                retrieved_items[index]=f"不可展开的图数据库节点{nid}的内容：{content}"
                del expandable_nodes[nid]
                backwards=gdb.get_neighbors(data_label,{"id":nid},direction="out")
                for item in backwards:
                    mid=item["id"]
                    sub_backwards=gdb.get_neighbors(data_label,{"id":mid},direction="out")
                    if len(sub_backwards)>0:
                        expandable_nodes[mid]=(len(retrieved_items),item["content"])
                        # print(f"\t可展开的节点[{mid}]内容：",item["content"])
                        retrieved_items.append(f"可展开的图数据库节点{mid}的内容：{item["summary"]}")
                    else:
                        retrieved_items.append(f"不可展开的图数据库节点{mid}的内容：{item["content"]}")
                
        
        return {**state,"retrieved_content":retrieved_items}
    return gdb_retrieve