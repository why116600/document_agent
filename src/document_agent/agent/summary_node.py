from pathlib import Path

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
            print("【参考资料】未提取到有效内容，改用用户输入作为目标")
            return {
                **state,
                "file_summaries": {},
                "retrieved_content": [],
                "retrieve_target": user_query,
            }

        # 3. 结合各文件角色、摘要与用户输入，智能提炼精准的待检索目标
        file_roles = state.get("file_roles") or {}
        user_intent = state.get("user_intent") or "new"
        rewrite_file = state.get("rewrite_file")

        summaries_lines = []
        for path, summary in summary_result.items():
            role_tag = file_roles.get(str(Path(path).resolve()), "参考资料")
            summaries_lines.append(f"- 文件路径：{path} [角色：{role_tag}]\n  文件摘要：{summary}")
        summaries_prompt = "\n".join(summaries_lines)

        task_desc = f"任务模式：{'改写已有文档 (REWRITE)' if user_intent == 'rewrite' else '全新文档撰写 (NEW)'}"
        if rewrite_file:
            task_desc += f"，待修改目标文档为：{Path(rewrite_file).name}"

        prompt = (
            "你是一个文档检索助手，你的任务是根据用户任务、用户指令和各文件摘要，提炼出需要在参考资料或目标文档中重点检索的核心目标。\n"
            f"【任务规划】{task_desc}\n"
            f"【文档清单与摘要】\n{summaries_prompt}\n"
            f"【用户对话内容】{user_query}\n"
            "请注意：若为改写任务，待修改文档内容后续将由改写引擎处理，检索重点通常应放在参考文档的排版规范、样式特征、补充数据或对照点上。\n"
            "仅输出提炼出的精准检索目标，不要输出其他客套内容。"
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