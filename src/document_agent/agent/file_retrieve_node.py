
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path
import traceback

try:
    from document_agent.agent.llm_client import llm_invoke, llm_model_invoke
    from document_agent.agent.agent_core import AgentState
except ImportError:
    from llm_client import llm_invoke, llm_model_invoke
    from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content

# 文件检索节点

def create_file_retrieve_node(llm):
    def file_retrive(state: AgentState) -> AgentState:
        params = state.get("retrieve_params") or {}
        retrieve_target = state.get("retrieve_target", "")
        file_retrieved_items = dict(state.get("file_retrived_items") or {})
        retrieved_items = list(state.get("retrieved_content") or [])

        path = params.get("path", "")
        query = params.get("query", "")

        if not path or not query:
            err = "file工具缺少必要参数(path或query)"
            print(f"【文件检索】{err}")
            return {**state, "error": err}

        # 检查是否重复检索（通过文件名与关键词匹配）
        for (p, q) in file_retrieved_items.keys():
            if (p == path or Path(str(p)).name.lower() == Path(str(path)).name.lower()) and q == query:
                msg = f"文件 {Path(path).name} 的关键词 '{query}' 此前已完成检索"
                print(f"【文件检索】{msg}，跳过重复提取")
                return {**state, "error": msg}

        path_obj = Path(path)
        # 如果路径不存在，尝试在 input_file_path 中找同名文件匹配完整路径
        if not path_obj.exists():
            matched = False
            for cand in (state.get("input_file_path") or []):
                if Path(cand).name.lower() == path_obj.name.lower():
                    path_obj = Path(cand)
                    path = str(path_obj)
                    matched = True
                    break
            if not matched:
                err = f"文件路径不存在：{path}"
                print(f"【文件检索】{err}")
                return {**state, "error": err}

        content = extract_document_content(str(path_obj))
        if content is None:
            err = f"不支持的文件类型：{path_obj.suffix}"
            print(f"【文件检索】{err}")
            return {**state, "error": err}

        print(f"【文件检索】正在检索文件 {path_obj.name}，检索目标: {query}")
        prompt_for_retrieval = (
            "你是一个文档检索助手，你的任务是根据用户提供的参考文件全文和检索目标，提炼出关键事实、数据和表述。\n\n"
            f"文件全文内容：\n{content}\n\n"
            f"检索目标/要点：{query}\n\n"
            "请只提炼与检索目标最相关的有效信息，语言简明扼要，直接输出提炼出的要点，不要输出无关客套话。"
        )
        res = llm_invoke(llm, prompt_for_retrieval)
        result = getattr(res, "content", "")
        if result:
            result = str(result).strip()
            retrieved_items.append(f"【参考文件 {path_obj.name} 检索结果 ({query})】：\n{result}")
            file_retrieved_items[(path, query)] = result
            print(f"【文件检索】提炼完成（共 {len(result)} 字）")

        return {
            **state,
            "file_retrived_items": file_retrieved_items,
            "retrieved_content": retrieved_items,
            "error": None,
        }

    return file_retrive