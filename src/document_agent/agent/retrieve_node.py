
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from pathlib import Path
import traceback

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.retrieve_tool.extract_document import extract_document_content
from document_agent.skills.plan import (
    begin_step,
    current_step,
    finalize_report,
    new_progress,
    record_assessment,
)
from document_agent.skills.render import allowed_tools, render_research_brief
from document_agent.skills.schema import DocumentSkill, coerce_skill


class RetrieveFuncCallable(BaseModel):
    tool_name: str = Field(description="要调用的工具名称")
    arguments: Dict[str, Any] = Field(description="调用工具所需的参数")


class EvidenceCoverage(BaseModel):
    """对一轮检索结果是否回答当前研究问题的判断。"""

    covered: bool = Field(description="现有资料是否足以回答该问题")
    reason: str = Field(default="", description="判断理由，指出已获得什么或还缺什么")


# 各检索工具的说明；Skill 通过来源优先级决定本次实际开放哪些工具
TOOL_DESCRIPTIONS = {
    "file": "file为检索用户提供的文件内容的工具，参数path为文件路径，参数query为检索的关键内容。",
    "knowledge": "knowledge为检索系统知识库的工具，融合规章制度流程规范与实体关系事实，参数query为要检索的关键内容。",
    "end": "end为结束检索的工具，如果你认为目前已知的内容已经满足用户要求，可以调用end工具结束检索。",
}


def build_tool_description(tools: List[str]) -> str:
    return "\n".join(TOOL_DESCRIPTIONS[tool] for tool in tools if tool in TOOL_DESCRIPTIONS)


def _assess_coverage(llm, step: Dict[str, Any], records: List[Dict[str, Any]]) -> tuple:
    """判断本轮新增证据是否回答了当前研究问题；无证据时不调用模型。"""
    if not records:
        return False, "本轮没有检索到新的证据"
    if llm is None:
        return False, "未配置模型，无法判断证据是否覆盖研究问题"

    lines = []
    for record in records[:10]:
        source = str(record.get("source") or "未知来源")
        snippet = str(record.get("snippet") or "").strip()
        lines.append(f"- 来源：{source}｜片段：{snippet}")
    evidence_text = "\n".join(lines)[:3000]

    prompt = (
        "你是检索结果审核助手。请判断下列资料是否足以回答给定的研究问题。\n"
        f"研究问题：{step['question']}\n"
        f"本轮资料：\n{evidence_text}\n"
        "判定标准：出现可引用的具体事实、数据或明确结论即算覆盖；只有标题、目录、泛泛相关或"
        "「未找到」提示不算覆盖。\n"
        "请给出 covered（是否覆盖）与简要 reason。"
    )
    result = llm_model_invoke(llm, prompt, EvidenceCoverage)
    if result is None:
        return False, "证据覆盖判断失败"
    return bool(result.covered), result.reason


def _orchestrated_retrieve(llm, state: AgentState, skill: DocumentSkill, plan: Dict[str, Any]) -> AgentState:
    """计划驱动检索：按队列逐条执行，评估覆盖情况，预算内继续或记录缺失。"""
    progress = dict(state.get("retrieval_progress") or new_progress())
    remaining = int(state.get("retrieve_count_limit") or 0)
    evidence = list(state.get("evidence_records") or [])

    # 1. 先评估上一轮检索结果
    if progress.get("pending"):
        pending = progress["pending"]
        step = pending["step"]
        start = int(pending.get("evidence_start") or 0)
        new_records = evidence[start:]
        covered, reason = _assess_coverage(llm, step, new_records)
        progress = record_assessment(plan, progress, covered, len(new_records))
        print(
            f"【检索评估】{step['question_id']}｜{step['source']}｜"
            f"{'已覆盖' if covered else '未覆盖'}｜{reason}"
        )

    # 2. 预算耗尽或计划执行完毕：输出检索报告并结束
    step = current_step(plan, progress)
    if step is None or remaining <= 0:
        budget_total = int(skill.research.max_rounds)
        report = finalize_report(plan, progress, budget_total, budget_total - remaining)
        print(f"【检索结果】覆盖 {len(report['covered'])}/{len(plan['questions'])} 个研究问题")
        if report["missing"]:
            print("【检索结果】未覆盖的问题：" + "、".join(report["missing"]))
        return {
            **state,
            "state": "start",
            "retrieve_tool": "end",
            "retrieval_progress": progress,
            "retrieval_report": report,
        }

    # 3. 下发下一步检索
    progress = begin_step(progress, plan, len(evidence))
    params: Dict[str, Any] = {"query": step["query"]}
    if step["source"] == "file":
        params["path"] = step["path"]
    scope = f"｜文件：{Path(step['path']).name}" if step.get("path") else ""
    print(f"【检索计划】{step['question_id']}｜{step['source']}｜{step['query']}{scope}")
    return {
        **state,
        "state": "start",
        "retrieval_progress": progress,
        "retrieve_tool": step["source"],
        "retrieve_params": params,
        "retrieve_target": step["query"],
        "retrieve_count_limit": remaining - 1,
    }


def _legacy_retrieve(llm, state: AgentState, skill) -> AgentState:
    """未启用 Skill（或 Skill 没有检索目标）时的原有 LLM 选工具流程。"""
    s = state["retrieve_target"]
    msg = state["messages"]
    file_summaries = state["file_summaries"]
    retrieve_limit = state["retrieve_count_limit"]
    retrived_conent = state.get("retrieved_content", [])
    file_retrived_item_set = set()  # 已经检索过的内容，避免重复检索
    tool_response = ""
    allowed = allowed_tools(skill)
    tool_lines = build_tool_description(allowed)
    research_brief = render_research_brief(skill)
    if research_brief:
        research_brief += "\n必须优先使用排在前面的来源；不要调用列表之外的工具。"
    if len(file_summaries) > 0:
        summaries_prompt = "\n".join([f"文件路径：{path}\n文件摘要：{summary}" for path, summary in file_summaries.items()])
        file_prompt_line = "用户提供的文件参考内容：" + summaries_prompt
    else:
        file_prompt_line = "用户没有提供任何文件参考内容，可使用其他工具检索"
    if retrieve_limit <= 0:
        print("超出检索次数")
        return {**state, "retrieve_tool": "end"}
    for _ in range(10):
        if len(retrived_conent) > 0:
            retrived_conent_str = "目前已知：\n" + "\n".join(retrived_conent)
        else:
            retrived_conent_str = "目前已知：\n无"
        think_prompt = (
            "你是一个文档写作辅助检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容，以供后续写文档使用。\n"
            f"{retrived_conent_str}\n"
            f"{file_prompt_line}\n"
            f"{research_brief}\n"
            f"用户对话内容：{msg}\n"
            "你有以下工具可以使用：\n"
            f"{tool_lines}\n"
            f"{tool_response}\n"
            "请分析后续使用工具的思路"
        )
        res = llm_invoke(llm, think_prompt)
        analysis = getattr(res, "content", "无")
        print("检索智能体的分析：", analysis)
        prompt = (
            "你是一个文档写作辅助检索助手，你的任务是根据用户提供的参考内容，检索出与用户问题相关的内容，以供后续写文档使用。\n"
            f"{retrived_conent_str}\n"
            f"{file_prompt_line}\n"
            f"{research_brief}\n"
            f"用户对话内容：{msg}\n"
            "你有以下工具可以使用：\n"
            f"{tool_lines}\n"
            f"{tool_response}\n"
            f"请严格按照以下分析选择使用的工具：\n{analysis}"
        )
        tool = llm_model_invoke(llm, prompt, RetrieveFuncCallable)
        if tool is None:
            print("检索工具调用失败，继续尝试...")
            continue
        if tool.tool_name not in allowed:
            tool_response = (f"工具反馈：{tool.tool_name} 不在当前 Skill 允许的来源范围内，"
                             f"只能使用 {allowed}。")
            print(tool_response)
            continue
        query = tool.arguments.get("query", "")
        if len(query) <= 0 and tool.tool_name != "end":
            print("无检索参数，继续尝试...")
            continue
        print("使用工具：", tool.tool_name, "参数：", tool.arguments)
        return {
            **state,
            "retrieve_tool": tool.tool_name,
            "retrieve_params": tool.arguments,
            "retrieve_target": query,
            "retrieve_count_limit": retrieve_limit - 1,
        }
    return {**state, "retrieved_content": retrived_conent, "state": "error"}


# 检索模块总调度
def create_retrieve_node(llm):
    def retrive_node(state: AgentState) -> AgentState:
        skill = coerce_skill(state.get("skill"))
        plan = state.get("retrieval_plan") or {}
        # 有计划（Skill 定义了研究问题/数据字段）时走计划驱动；否则保持原有 LLM 选工具流程
        if skill is not None and plan.get("questions"):
            return _orchestrated_retrieve(llm, state, skill, plan)
        return _legacy_retrieve(llm, state, skill)

    return retrive_node
