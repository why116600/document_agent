
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from datetime import datetime
from pathlib import Path
import traceback

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from retrieval_material import select_source_quote
from document_agent.retrieve_tool.extract_document import extract_document_content
from document_agent.skills.schema import coerce_skill

MAX_FILE_RETRIEVE_CHARS = 2000  # 单次文件检索结果上限，防止提示词被单段撑爆


def _contains_normalized(text: str, needle: str) -> bool:
    """忽略空白后判断 needle 是否已在 text 中，避免同一原文摘录重复注入。"""
    def _norm(value):
        return "".join(str(value or "").split())
    return bool(needle) and _norm(needle) in _norm(text)

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
        skill=coerce_skill(state.get("skill"))
        cached_result=file_retrieved_items.get((path,query))
        print(f"对文件{path}进行检索")
        if cached_result is not None and skill is None:
            tool_response=f"重复对文件{path}检索：{query}"
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
        # Skill 可限制单个文件送入模型的字符数，避免超长文件把提示词撑爆
        max_chars=int(skill.research.file.max_chars) if skill is not None else 0
        if max_chars and len(content)>max_chars:
            print(f"【文件检索】{path} 内容超过 {max_chars} 字符，已截断")
            content=content[:max_chars]+"\n…（内容超长，已截断）"
        prompt_for_retrieval=(
            "你是一个文档检索助手，你的任务是根据用户提供的参考内容和用户对话检索出符合目标的内容。\n"
            f"用户提供的文件内容：{content}\n"
            f"要检索的内容：{query}\n"
            "请按以下格式输出，且只输出这两部分：\n"
            "【原文摘录】逐句抄录与检索目标直接相关的原文句子；必须连同紧邻的限定条件、起算点、"
            "适用范围、否定关系和例外一并保留；不要只摘第一句，不要改写、合并或省略限定语。\n"
            "【检索回答】用简短文字回答检索目标，只能使用原文摘录中的事实，不得补充原文没有的信息。"
        )
        if cached_result is not None:
            # 不同研究问题可使用相同查询；复用提取结果，再按当前问题评估覆盖。
            result=cached_result
        else:
            res=llm_invoke(llm, prompt_for_retrieval)
            raw=str(getattr(res,"content","") or "").strip()
            answer=raw.replace("【检索回答】","").strip()
            # 模型回答可能只摘第一句；这里用原文摘录兜底，确保紧邻限定句进入素材。
            quote=select_source_quote(content,query)
            quote_body=quote.replace("【原文摘录】","").strip() if quote else ""
            parts=[]
            if quote and not _contains_normalized(answer,quote_body):
                parts.append(quote)
            if answer:
                parts.append("【检索回答】\n"+answer)
            result="\n".join(parts).strip()
            if len(result)>MAX_FILE_RETRIEVE_CHARS:
                result=result[:MAX_FILE_RETRIEVE_CHARS]
            print(f"【文件检索】原文摘录={'有' if quote else '无'} 结果字数={len(result)}")
        if len(result)>0:
            retrieved_items.append(f"使用file工具以{query}为关键内容，对文件{path}的检索结果：{result}")
            file_retrieved_items[(path,query)]=result
            evidence_records=list(state.get("evidence_records") or [])
            path_obj=Path(path)
            try:
                file_mtime=datetime.fromtimestamp(path_obj.stat().st_mtime).isoformat()
            except OSError:
                file_mtime=""
            evidence_records.append({
                "source_type":"file",
                "source":str(path),
                "sources":[str(path)],
                "title":path_obj.stem,
                "suffix":path_obj.suffix.lower(),
                "file_mtime":file_mtime,
                "query":str(query),
                "snippet":str(result)[:300],
                "content":str(result)[:6000],
                "source_excerpt":str(content)[:10000],
            })
            return {**state,"file_retrived_items":file_retrieved_items,"retrieved_content":retrieved_items,
                    "evidence_records":evidence_records}
        return {**state,"file_retrived_items":file_retrieved_items,"retrieved_content":retrieved_items}
    return file_retrive
