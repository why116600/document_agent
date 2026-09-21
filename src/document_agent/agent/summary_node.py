from pathlib import Path

try:
    from document_agent.agent.llm_client import llm_invoke, llm_model_invoke
    from document_agent.agent.agent_core import AgentState, get_last_user_text
except ImportError:
    from llm_client import llm_invoke, llm_model_invoke
    from agent_core import AgentState, get_last_user_text
from document_agent.retrieve_tool.extract_document import extract_document_content

# 参考文件总字数阈值：低于该阈值时直接全文注入上下文，免去繁琐的摘要与检索 Agent 循环（约 3000~4000 tokens）
MAX_DIRECT_REF_CHARS = 6000


def create_summary_node(llm):
    def summary_node(state: AgentState) -> AgentState:
        s = state["messages"]
        files_to_summarize = state.get("input_file_path") or []
        user_query = get_last_user_text(s)

        if not files_to_summarize and not state.get("file_summaries"):
            print("【参考资料】未提供外部参考文件，跳过摘要与检索")
            return {**state, "file_summaries": {}, "retrieved_content": [], "retrieve_target": user_query}

        # 1. 先尝试直接提取所有参考文件的完整文本
        raw_contents = {}
        total_chars = 0
        for path in files_to_summarize:
            content = extract_document_content(path)
            if content:
                raw_contents[path] = content
                total_chars += len(content)
            else:
                print(f"【参考资料】无法提取文件内容或不支持的格式：{path}")

        if not raw_contents:
            print("【参考资料】未能提取到有效的参考文件内容，跳过检索")
            return {**state, "file_summaries": {}, "retrieved_content": [], "retrieve_target": user_query}

        # 2. 短/中型参考文件直通车：字数适中时，直接作为全文素材注入，避免信息损耗与多轮检索开销
        if total_chars <= MAX_DIRECT_REF_CHARS:
            retrieved_items = [
                f"【参考文件：{Path(p).name}】\n{c}" for p, c in raw_contents.items()
            ]
            print(f"【参考资料】已加载 {len(raw_contents)} 份参考文件（共约 {total_chars} 字），直接载入全文供写作/改写使用（免去检索中间环节）")
            file_summaries = {p: f"(全文直接载入，共约{len(c)}字)" for p, c in raw_contents.items()}
            return {
                **state,
                "file_summaries": file_summaries,
                "retrieved_content": retrieved_items,
                "retrieve_target": user_query,
            }

        # 3. 超长参考文件：采用分篇大模型摘要 + 后续智能检索
        print(f"【参考资料】参考文件总字数约 {total_chars} 字，超过直通阈值（{MAX_DIRECT_REF_CHARS}字），启用大文档摘要压缩与分段检索")
        summary_result = dict(state.get("file_summaries") or {})
        for path, content in raw_contents.items():
            if path in summary_result:
                continue
            prompt = (
                "你是一个文档摘要助手，你的任务是根据用户提供的文档内容，生成该文档的摘要。\n"
                f"用户提供的文档内容：{content}\n"
                "请生成该文档的摘要，涵盖核心论点与关键数据，仅输出结果。"
            )
            res = llm_invoke(llm, prompt)
            result = getattr(res, "content", None)
            if result:
                summary_result[path] = result
            else:
                summary_result[path] = content[:500] + "..."

        # 检索目标直接采用用户真实需求，无需再空转一次大模型复读
        return {
            **state,
            "file_summaries": summary_result,
            "retrieved_content": [],
            "retrieve_target": user_query,
        }

    return summary_node