
from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content


def get_last_user_text(messages) -> str:
    """取出最后一条用户消息的文本，兼容消息对象与字典两种形式。"""
    for message in reversed(list(messages or [])):
        if isinstance(message, dict):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        elif getattr(message, "type", "") == "human":
            return str(getattr(message, "content", "") or "")
    return ""


def create_summary_node(llm):
    def summary_node(state : AgentState) -> AgentState:
        s=state["messages"]
        files_to_summarize=state.get('input_file_path') or []
        if not files_to_summarize and not state.get('file_summaries'):
            #没有参考文件时不需要生成摘要，直接用用户的要求作为检索目标
            retrieve_target=get_last_user_text(s)
            print("没有提供参考文件，跳过摘要，检索目标：",retrieve_target)
            return {**state,"file_summaries":{},"retrieve_target":retrieve_target}
        summary_result=dict(state.get('file_summaries') or {})
        for path in files_to_summarize:
            if path in state['file_summaries']:
                print(f"文件已存在摘要，跳过：{path}")
                continue
            content=extract_document_content(path)
            if content is None:
                print(f"不支持的文件类型：{path}")
                continue
            prompt=(
                "你是一个文档摘要助手，你的任务是根据用户提供的文档内容，生成该文档的摘要。\n"
                f"用户提供的文档内容：{content}\n"
                "请生成该文档的摘要，仅输出结果。"
            )
            res=llm_invoke(llm, prompt)
            result=getattr(res,"content",None)
            if result is None:
                print(f"摘要生成失败：{path}")
                continue
            summary_result[path]=result
        summaries_prompt="\n".join([f"文件路径：{path}\n文件摘要：{summary}" for path, summary in summary_result.items()])
        prompt=(
            "你是一个文档检索助手，你的任务是根据用户对话和用户提供的文档及其摘要判断用户想要检索的目标内容。\n"
            f"{summaries_prompt}"
            f"用户对话内容：{s}\n"
            "仅输出结果，不要输出其他内容。"
        )
        res=llm_invoke(llm, prompt)
        result=getattr(res,"content","")
        print("检索目标内容：", result)
        return {**state, "file_summaries": summary_result,"retrieve_target":result}
    return summary_node