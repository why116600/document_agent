
from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content

def create_summary_node(llm):
    def summary_node(state : AgentState) -> AgentState:
        s=state["messages"]
        files_to_summarize=state['input_file_path']
        summary_result={}
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