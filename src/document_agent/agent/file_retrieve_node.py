
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path
import traceback

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content

# 文件检索节点

def create_file_retrieve_node(llm):
    def file_retrive(state : AgentState) -> AgentState:
        params=state["retrieve_params"]
        retrieve_target=state["retrieve_target"]
        file_retrieved_items=state.get("file_retrived_items",[])
        retrieved_items=state["retrieved_content"]
        if params is None:
            return {**state,"state":"error","error":"文件检索缺少参数"}
        if not "path" in params.keys() or not "query" in params.keys():
            return {**state,"state":"error","error":"文件检索缺少必要参数字段"}
        path=params.get("path")
        query=params.get("query")
        print(f"对文件{path}进行检索")
        if (path,query) in file_retrieved_items.keys():
            tool_response=f"重复对文件{path}检索：",query
            print(tool_response)
            retrieved_items.append(tool_response)
            return {**state,"state":"error","error":"文件检索出现相同内容","retrieved_content":retrieved_items}
        path_obj=Path(path)
        if len(path)<=0:
            print("文件路径为空，继续尝试...")
            tool_response=f"file工具反馈：文件路径为空"
            retrieved_items.append(tool_response)
            return {**state,"state":"error","error":tool_response,"retrieved_content":retrieved_items}
        if not path_obj.exists():
            print(f"文件路径不存在：{path}")
            tool_response=f"file工具反馈：文件路径不存在：{path}"
            retrieved_items.append(tool_response)
            return {**state,"state":"error","error":tool_response,"retrieved_content":retrieved_items}
        content=extract_document_content(path)
        if content is None:
            print(f"不支持的文件类型：{path_obj.suffix}")
            tool_response=f"file工具反馈：不支持的文件类型：{path_obj.suffix}"
            retrieved_items.append(tool_response)
            return {**state,"state":"error","error":tool_response,"retrieved_content":retrieved_items}
        prompt_for_retrieval=(
            "你是一个文档检索助手，你的任务是根据用户提供的参考内容和用户对话检索出符合目标的内容。\n"
            f"用户提供的文件内容：{content}\n"
            f"要检索的内容：{query}\n"
            "仅回答问题，不要输出其他内容。"
        )
        res=llm_invoke(llm, prompt_for_retrieval)
        result=getattr(res,"content","")
        if len(result)>0:
            retrieved_items.append(f"使用file工具以{query}为关键内容，对文件{path}的检索结果：{result}")
            file_retrieved_items[(path,query)]=result
        return {**state,"file_retrived_items":file_retrieved_items,"retrieved_content":retrieved_items}
    return file_retrive