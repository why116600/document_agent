from pathlib import Path

try:
    from document_agent.agent.llm_client import llm_invoke
    from document_agent.agent.agent_core import AgentState, get_last_user_text
except ImportError:
    from llm_client import llm_invoke
    from agent_core import AgentState, get_last_user_text
from document_agent.retrieve_tool.extract_document import extract_document_content


def create_summary_node(llm):
    def summary_node(state: AgentState) -> AgentState:
        s = state["messages"]
        files_to_summarize = state.get("input_file_path") or []
        user_query = get_last_user_text(s)

        # 1. 没有参考文件时，直接跳过摘要阶段，将用户原始要求作为检索目标
        if not files_to_summarize and not state.get("file_summaries"):
            print("【参考资料】未提供参考文件，跳过摘要阶段")
            return {
                **state,
                "file_summaries": {},
                "retrieved_content": [],
                "retrieve_target": user_query,
            }

        # 2. 逐一提取并生成文件摘要
        summary_result = dict(state.get("file_summaries") or {})
        for path in files_to_summarize:
            if path in summary_result:
                continue
            content = extract_document_content(path)
            if not content:
                print(f"【参考资料】不支持或无法读取文件：{path}")
                continue

            prompt = (
                "你是一个专业的文档摘要助手，你的任务是提炼用户文档的核心内容。\n"
                f"文档内容：\n{content}\n"
                "请生成该文档的结构化摘要，涵盖核心主题、主要章节目录与关键数据，仅输出摘要文本。"
            )
            res = llm_invoke(llm, prompt)
            result = getattr(res, "content", None)
            if result:
                summary_result[path] = str(result).strip()
            else:
                summary_result[path] = content[:300] + "..."

        if not summary_result:
            print("【参考资料】未能提取到有效的参考文件内容，直接采用用户输入作为目标")
            return {
                **state,
                "file_summaries": {},
                "retrieved_content": [],
                "retrieve_target": user_query,
            }

        # 3. 结合各文件摘要与用户输入，智能提炼精准的待检索目标
        summaries_prompt = "\n".join([f"文件路径：{path}\n文件摘要：{summary}" for path, summary in summary_result.items()])
        prompt = (
            "你是一个文档检索助手，你的任务是根据用户对话和用户提供的文档及其摘要判断用户想要检索的目标内容。\n"
            f"{summaries_prompt}\n"
            f"用户对话内容：{user_query}\n"
            "仅输出提炼出的检索目标，不要输出其他客套内容。"
        )
        res = llm_invoke(llm, prompt)
        retrieve_target = getattr(res, "content", "").strip() or user_query
        print(f"【检索目标提炼】{retrieve_target}")

        return {
            **state,
            "file_summaries": summary_result,
            "retrieved_content": [],
            "retrieve_target": retrieve_target,
        }

    return summary_node