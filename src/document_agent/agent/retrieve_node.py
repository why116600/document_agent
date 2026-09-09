
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path
import traceback

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content

class RetrieveFuncCallable(BaseModel):
    tool_name: str = Field(description="要调用的工具名称")
    arguments: Dict[str, Any] = Field(description="调用工具所需的参数")

def create_retrieve_node(llm):
    def retrive_node(state : AgentState) -> AgentState:
        s = state["retrieve_target"]
        retrived_conent=[]
        file_retrived_item_set=set()#已经检索过的内容，避免重复检索
        tool_response=""
        summaries_prompt="\n".join([f"文件路径：{path}\n文件摘要：{summary}" for path, summary in state["file_summaries"].items()])
        # retrieve_llm=llm.with_structured_output(RetrieveFuncCallable,method="function_calling", include_raw=True)
        for _ in range(10):
            if len(retrived_conent)>0:
                retrived_conent_str="目前已知：\n"+"\n".join(retrived_conent)
            else:
                retrived_conent_str="目前已知：\n无"
            think_prompt = (
                "你是一个文档检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容。\n"
                f"{retrived_conent_str}\n"
                f"用户提供的文件参考内容：{summaries_prompt}\n"
                f"要检索的内容：{s}\n"
                "你有以下工具可以使用：\n"
                "file为检索用户提供的文件内容的工具，参数path为文件路径，参数query为检索的关键内容。\n"
                "end为结束检索的工具，如果你认为已经检索到足够的内容，可以调用end工具结束检索。\n"
                f"{tool_response}\n"
                "请分析后续使用工具的思路"
            )
            res=llm_invoke(llm,think_prompt)
            analysis=getattr(res,"content","无")
            print("检索智能体的分析：",analysis)
            prompt = (
                "你是一个文档检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容。\n"
                f"{retrived_conent_str}\n"
                f"用户提供的文件参考内容：{summaries_prompt}\n"
                f"要检索的内容：{s}\n"
                "你有以下工具可以使用：\n"
                "file为检索用户提供的文件内容的工具，参数path为文件路径，参数query为检索的关键内容。\n"
                "end为结束检索的工具，如果你认为已经检索到足够的内容，可以调用end工具结束检索。\n"
                f"{tool_response}\n"
                f"请严格按照以下分析选择使用的工具：\n{analysis}"
            )
            tool=llm_model_invoke(llm, prompt, RetrieveFuncCallable)
            if tool is None:
                print("检索工具调用失败，继续尝试...")
                continue
            print("使用工具：", tool.tool_name, "参数：", tool.arguments)
            if tool.tool_name=="file":
                path=tool.arguments.get("path")
                query=tool.arguments.get("query")
                if (path,query) in file_retrived_item_set:
                    print(f"重复对文件{path}检索：",query)
                    continue
                path_obj=Path(path)
                if len(path)<=0:
                    print("文件路径为空，继续尝试...")
                    tool_response=f"file工具反馈：文件路径为空"
                    continue
                if not path_obj.exists():
                    print(f"文件路径不存在：{path}")
                    tool_response=f"file工具反馈：文件路径不存在：{path}"
                    continue
                content=extract_document_content(path)
                if content is None:
                    print(f"不支持的文件类型：{path_obj.suffix}")
                    tool_response=f"file工具反馈：不支持的文件类型：{path_obj.suffix}"
                    continue
                file_retrived_item_set.add((path,query))
                prompt_for_retrieval=(
                    "你是一个文档检索助手，你的任务是根据用户提供的参考内容和用户对话检索出符合目标的内容。\n"
                    f"用户提供的文件内容：{content}\n"
                    f"用户对话内容：{s}\n"
                    f"要检索的内容：{query}\n"
                    "仅回答问题，不要输出其他内容。"
                )
                res=llm_invoke(llm, prompt_for_retrieval)
                result=getattr(res,"content","")
                if len(result)>0:
                    retrived_conent.append(f"使用file工具以{query}为关键内容，对文件{path}的检索结果：{result}")
                # print(f"检索文件{path}的结果：{result}")
            elif tool.tool_name=="end":
                print("检索结束")
                return {**state, "retrieved_content": retrived_conent}
            else:
                print(f"未知工具：{tool.tool_name}")
                tool_response=f"未知工具：{tool.tool_name}"
        return {**state, "retrieved_content": retrived_conent,"state":"error"}
    return retrive_node