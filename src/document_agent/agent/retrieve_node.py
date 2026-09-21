
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path

try:
    from document_agent.agent.llm_client import llm_invoke, llm_model_invoke
    from document_agent.agent.agent_core import AgentState
except ImportError:
    from llm_client import llm_invoke, llm_model_invoke
    from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content

class RetrieveFuncCallable(BaseModel):
    reason: str = Field(default="", description="检索思路与下一步计划")
    tool_name: str = Field(description="要调用的工具名称，可选值：file（检索指定文件）或 end（结束检索）")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="调用工具所需的参数")


def create_retrieve_node(llm):
    def retrive_node(state: AgentState) -> AgentState:
        # 如果前面 summary_node 已经通过直通车载入了参考资料全文，直接跳过检索 Agent
        if state.get("retrieved_content"):
            print(f"【参考资料】已直接载入 {len(state['retrieved_content'])} 份参考素材全文，无需启动检索智能体")
            return state

        s = state["retrieve_target"]
        retrived_conent = []
        file_retrived_item_set = set()  # 已经检索过的内容，避免重复检索
        tool_response = ""
        summaries_prompt = "\n".join([f"文件路径：{path}\n文件摘要：{summary}" for path, summary in state.get("file_summaries", {}).items()])
        if not state.get("file_summaries"):
            print("没有可检索的参考文件，跳过检索")
            return {**state, "retrieved_content": []}

        for round_idx in range(5):
            if len(retrived_conent) > 0:
                retrived_conent_str = "目前已知：\n" + "\n".join(retrived_conent)
                end_hint = "【提示】：当前已检索到参考材料。若材料已基本充足，请在 tool_name 中选择 'end' 结束检索并进入写作！\n"
            else:
                retrived_conent_str = "目前已知：\n无"
                end_hint = ""

            prompt = (
                "你是一个文档检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容。\n"
                f"{retrived_conent_str}\n"
                f"用户提供的文件参考内容：{summaries_prompt}\n"
                f"要检索的内容：{s}\n"
                "你有以下工具可以使用：\n"
                "1. file: 检索用户提供的文件内容，参数 path 为文件路径，参数 query 为检索的关键内容。\n"
                "2. end: 结束检索（材料已充足或已完成检索时调用）。\n"
                f"{end_hint}"
                f"{tool_response}\n"
                "请在 reason 中写出你的一两句简要思考，并在 tool_name 和 arguments 中指定要调用的工具。"
            )
            tool = llm_model_invoke(llm, prompt, RetrieveFuncCallable)
            if tool is None:
                print("检索工具调用失败，继续尝试...")
                continue
            if tool.reason:
                print(f"【检索思考】{tool.reason}")
            tool_name = (tool.tool_name or "").strip().lower()
            print("使用工具：", tool_name, "参数：", tool.arguments)
            if tool_name == "file":
                path = tool.arguments.get("path") or ""
                query = tool.arguments.get("query") or ""
                if (path, query) in file_retrived_item_set:
                    print(f"重复对文件{path}检索：", query)
                    # 已检索过该内容，如果已有检索结果则直接结束，避免死循环
                    if retrived_conent:
                        print("检测到重复检索且已有检索内容，自动结束检索")
                        return {**state, "retrieved_content": retrived_conent}
                    continue
                path_obj = Path(path)
                if len(path) <= 0:
                    print("文件路径为空，继续尝试...")
                    tool_response = "file工具反馈：文件路径为空"
                    continue
                if not path_obj.exists():
                    print(f"文件路径不存在：{path}")
                    tool_response = f"file工具反馈：文件路径不存在：{path}"
                    continue
                content = extract_document_content(path)
                if content is None:
                    print(f"不支持的文件类型：{path_obj.suffix}")
                    tool_response = f"file工具反馈：不支持的文件类型：{path_obj.suffix}"
                    continue
                file_retrived_item_set.add((path, query))
                prompt_for_retrieval = (
                    "你是一个文档检索助手，你的任务是根据用户提供的参考内容和用户对话检索出符合目标的内容。\n"
                    f"用户提供的文件内容：{content}\n"
                    f"用户对话内容：{s}\n"
                    f"要检索的内容：{query}\n"
                    "仅回答问题，不要输出其他内容。"
                )
                res = llm_invoke(llm, prompt_for_retrieval)
                result = getattr(res, "content", "")
                if len(result) > 0:
                    retrived_conent.append(f"使用file工具以{query}为关键内容，对文件{path}的检索结果：{result}")
            elif tool_name == "end":
                print("检索结束")
                return {**state, "retrieved_content": retrived_conent}
            else:
                print(f"未知工具：{tool.tool_name}")
                tool_response = f"未知工具：{tool.tool_name}"
        print(f"检索轮次结束，共获取 {len(retrived_conent)} 条有效素材，进入后续流程")
        return {**state, "retrieved_content": retrived_conent}
    return retrive_node