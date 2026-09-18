
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

#检索模块总调度
def create_retrieve_node(llm):
    def retrive_node(state : AgentState) -> AgentState:
        s = state["retrieve_target"]
        msg=state["messages"]
        file_summaries=state["file_summaries"]
        retrieve_limit=state["retrieve_count_limit"]
        retrived_conent=state.get("retrieved_content",[])
        file_retrived_item_set=set()#已经检索过的内容，避免重复检索
        tool_response=""
        if len(file_summaries)>0:
            summaries_prompt="\n".join([f"文件路径：{path}\n文件摘要：{summary}" for path, summary in file_summaries.items()])
            file_prompt_line="用户提供的文件参考内容："+summaries_prompt
        else:
            file_prompt_line="用户没有提供任何文件参考内容，可使用其他工具检索"
        if retrieve_limit<=0:
            print("超出检索次数")
            return {**state, "retrieve_tool":"end"}
        # retrieve_llm=llm.with_structured_output(RetrieveFuncCallable,method="function_calling", include_raw=True)
        for _ in range(10):
            if len(retrived_conent)>0:
                retrived_conent_str="目前已知：\n"+"\n".join(retrived_conent)
            else:
                retrived_conent_str="目前已知：\n无"
            think_prompt = (
                "你是一个文档写作辅助检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容，以供后续写文档使用。\n"
                f"{retrived_conent_str}\n"
                f"{file_prompt_line}\n"
                f"用户对话内容：{msg}\n"
                # f"要检索的内容：{s}\n"
                "你有以下工具可以使用：\n"
                "file为检索用户提供的文件内容的工具，参数path为文件路径，参数query为检索的关键内容。\n"
                "knowledge为检索系统知识库的工具，包含行业知识、工作流程、硬性规范，参数key为要检索的关键内容，参数expand为要展开的节点编号。\n"
                "end为结束检索的工具，如果你认为目前已知的内容已经满足用户要求，可以调用end工具结束检索。\n"
                f"{tool_response}\n"
                "请分析后续使用工具的思路"
            )
            res=llm_invoke(llm,think_prompt)
            analysis=getattr(res,"content","无")
            print("检索智能体的分析：",analysis)
            prompt = (
                "你是一个文档写作辅助检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容，以供后续写文档使用。\n"
                f"{retrived_conent_str}\n"
                f"{file_prompt_line}\n"
                f"用户对话内容：{msg}\n"
                # f"要检索的内容：{s}\n"
                "你有以下工具可以使用：\n"
                "file为检索用户提供的文件内容的工具，参数path为文件路径，参数query为检索的关键内容。\n"
                "knowledge为检索系统知识库的工具，包含行业知识、工作流程、硬性规范，参数query为要检索的关键内容，参数expand为要展开的节点编号。\n"
                "end为结束检索的工具，如果你认为目前已知的内容已经满足用户要求，可以调用end工具结束检索。\n"
                f"{tool_response}\n"
                f"请严格按照以下分析选择使用的工具：\n{analysis}"
            )
            tool=llm_model_invoke(llm, prompt, RetrieveFuncCallable)
            if tool is None:
                print("检索工具调用失败，继续尝试...")
                continue
            query=tool.arguments.get("query","")
            if len(query)<=0 and tool.tool_name!="end":
                print("无检索参数，继续尝试...")
                continue
            print("使用工具：", tool.tool_name, "参数：", tool.arguments)
            return {**state, "retrieve_tool":tool.tool_name,"retrieve_params":tool.arguments,"retrieve_target":query,"retrieve_count_limit":retrieve_limit-1}
        return {**state, "retrieved_content": retrived_conent,"state":"error"}
    return retrive_node