
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path

try:
    from document_agent.agent.llm_client import llm_invoke, llm_model_invoke
    from document_agent.agent.agent_core import AgentState, get_last_user_text
except ImportError:
    from llm_client import llm_invoke, llm_model_invoke
    from agent_core import AgentState, get_last_user_text
from document_agent.retrieve_tool.extract_document import extract_document_content

class RetrieveFuncCallable(BaseModel):
    reason: str = Field(default="", description="检索思路与下一步计划")
    tool_name: str = Field(description="要调用的工具名称，可选值：file（检索指定文件）、knowledge（检索系统知识库）或 end（结束检索）")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="调用工具所需的参数")


#检索模块总调度
def create_retrieve_node(llm):
    def retrive_node(state: AgentState) -> AgentState:
        file_summaries = state.get("file_summaries") or {}
        gdb_label = state.get("gdb_label")
        retrieved_content = list(state.get("retrieved_content") or [])
        file_retrieved_items = dict(state.get("file_retrived_items") or {})
        retrieve_limit = state.get("retrieve_count_limit")
        if retrieve_limit is None:
            retrieve_limit = 5

        # 1. 退出条件检查：无参考文件且无图数据库需求，直接结束检索进入后续阶段
        if not file_summaries and not gdb_label:
            print("【检索智能体】无参考资料或知识库检索需求，跳过检索直接进入写作阶段")
            return {
                **state,
                "retrieve_tool": "end",
                "retrieve_params": {},
                "retrieve_target": "",
                "error": None,
            }

        # 2. 退出条件检查：检索次数已达上限
        if retrieve_limit <= 0:
            print("【检索智能体】已达到最大检索次数限制，结束检索")
            return {
                **state,
                "retrieve_tool": "end",
                "retrieve_params": {},
                "retrieve_target": "",
                "error": None,
            }

        user_query = get_last_user_text(state.get("messages"))

        # 组装可用参考文件信息
        if file_summaries:
            summaries_prompt = "\n".join([f"- 文件路径：{path}\n  文件摘要：{summary}" for path, summary in file_summaries.items()])
            file_prompt_line = f"用户提供的参考文件清单与摘要：\n{summaries_prompt}"
        else:
            file_prompt_line = "用户未提供参考文件。"

        # 组装已知信息
        if retrieved_content:
            retrieved_content_str = "目前已检索到的参考信息：\n" + "\n".join([f"- {item}" for item in retrieved_content])
        else:
            retrieved_content_str = "目前已检索到的参考信息：\n无"

        # 组装工具执行历史反馈
        tool_response = ""
        if state.get("error"):
            tool_response = f"上一轮工具执行反馈：{state['error']}\n"

        # =====================================================================
        # 【阶段一：先思考（Think）】
        # 由大模型进行深入的思维链推理：评估当前已知信息完整度、是否需进一步检索、以及工具选择思路
        # =====================================================================
        think_prompt = (
            "你是一个文档写作辅助检索助手，你的任务是根据用户提供的参考内容和对话，检索出与用户写作任务最相关的内容，以供后续写文档使用。\n\n"
            f"{retrieved_content_str}\n\n"
            f"{file_prompt_line}\n\n"
            f"用户对话与指令：{user_query}\n\n"
            "你有以下工具可以使用：\n"
            "1. file：检索用户提供的文件内容。参数 path 为文件路径，参数 query 为检索的关键内容。\n"
            "2. knowledge：检索系统知识库。包含行业知识、工作流程、硬性规范，参数 query 为要检索的关键内容，参数 expand 为要展开的节点编号。\n"
            "3. end：结束检索的工具。如果你认为目前已知的内容已经满足用户要求，可以调用 end 工具结束检索。\n\n"
            f"{tool_response}"
            "请深入分析当前检索状态：目前已知的信息是否充足？是否还需要调用工具进一步检索特定事实/段落？请详细输出你的后续思考与工具调用思路："
        )
        think_res = llm_invoke(llm, think_prompt)
        analysis = getattr(think_res, "content", "信息已足够或无需进一步检索")
        print("【检索智能体的分析思考】\n", analysis)

        # =====================================================================
        # 【阶段二：后执行（Act 决策）】
        # 将上一阶段的思维链分析结果作为强上下文输入，引导大模型进行精准结构化工具调用
        # =====================================================================
        prompt = (
            "你是一个文档写作辅助检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容，以供后续写文档使用。\n\n"
            f"{retrieved_content_str}\n\n"
            f"{file_prompt_line}\n\n"
            f"用户对话与指令：{user_query}\n\n"
            f"你刚刚完成的检索思路分析：\n{analysis}\n\n"
            "你有以下工具可以使用：\n"
            "1. file：检索用户提供的文件内容。参数 path 为文件完整路径，参数 query 为要针对该文件检索的关键内容/关键词。\n"
            "2. knowledge：检索系统知识库。参数 query 为检索关键词，参数 expand 为需要展开的节点ID。\n"
            "3. end：结束检索。如果你认为目前已知的信息已经充分满足用户写作要求，或者没有更多需要检索的内容，请调用 end 结束检索。\n\n"
            f"{tool_response}"
            "请严格结合你前面的思考分析，在 reason 中写出你的一两句简要结论，并在 tool_name 和 arguments 中指定要调用的工具。"
        )

        for attempt in range(5):
            tool = llm_model_invoke(llm, prompt, RetrieveFuncCallable)
            if tool is None:
                print(f"【检索智能体】第 {attempt+1} 次工具调用解析失败，重试...")
                continue

            tool_name = str(tool.tool_name).strip().lower()
            reason = tool.reason or ""

            if tool_name == "end":
                print(f"【检索智能体】决定结束检索。理由: {reason or '参考信息已满足需求'}")
                return {
                    **state,
                    "retrieve_tool": "end",
                    "retrieve_params": {},
                    "retrieve_target": "",
                    "retrieve_count_limit": retrieve_limit - 1,
                    "error": None,
                }

            if tool_name == "file":
                args = tool.arguments or {}
                path = args.get("path", "")
                query = args.get("query", "")
                if not path or not query:
                    print(f"【检索智能体】file 工具参数缺失 (path: {path}, query: {query})，重试...")
                    continue

                # 检查是否重复调用相同文件和查询，避免死循环
                is_duplicate = False
                for (p, q) in file_retrieved_items.keys():
                    if Path(str(p)).name.lower() == Path(str(path)).name.lower() and q == query:
                        is_duplicate = True
                        break

                if is_duplicate:
                    print(f"【检索智能体】检测到重复检索需求 ({path}, {query})，参考内容已存在，自动结束检索。")
                    return {
                        **state,
                        "retrieve_tool": "end",
                        "retrieve_params": {},
                        "retrieve_target": "",
                        "retrieve_count_limit": retrieve_limit - 1,
                        "error": None,
                    }

                print(f"【检索智能体】调用 file 工具 -> 文件: {path}，检索点: {query} (思路: {reason})")
                return {
                    **state,
                    "retrieve_tool": "file",
                    "retrieve_params": args,
                    "retrieve_target": query,
                    "retrieve_count_limit": retrieve_limit - 1,
                    "error": None,
                }

            if tool_name == "knowledge":
                args = tool.arguments or {}
                query = args.get("query", "") or args.get("key", "")
                print(f"【检索智能体】调用 knowledge 工具 -> 检索: {query} (思路: {reason})")
                return {
                    **state,
                    "retrieve_tool": "knowledge",
                    "retrieve_params": args,
                    "retrieve_target": query,
                    "retrieve_count_limit": retrieve_limit - 1,
                    "error": None,
                }

        # 多次尝试均未能产生有效工具调用，安全兜底结束检索
        print("【检索智能体】未产生有效新工具调用，安全兜底结束检索，进入后续写作阶段。")
        return {
            **state,
            "retrieve_tool": "end",
            "retrieve_params": {},
            "retrieve_target": "",
            "error": None,
        }
    return retrive_node