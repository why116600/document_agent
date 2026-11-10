"""Skill 解析节点：匹配、加载校验、能力预检与检索计划生成。

位置在 intent 之后、summary 之前：intent 分析任务并选择规范，本节点复用选择结果并检查能力，
后续 summary / retrieve / docx / rewrite / verify 全部从 state 读取已校验的配置与计划。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_core import AgentState, get_last_user_text
from llm_client import llm_model_invoke
from document_agent.skills.match import resolve_skill
from document_agent.skills.plan import (
    RetrievalPlanDraft,
    build_plan_prompt,
    build_retrieval_plan,
    new_progress,
)
from document_agent.skills.schema import DocumentSkill, coerce_skill
from document_agent.skills.store import SkillError


def _probe_knowledge(gdb: Any) -> Tuple[bool, str]:
    """检查知识库检索是否可用；只做轻量连通性探测，不执行真实查询。"""
    if gdb is None:
        return False, "图数据库未初始化"
    driver = getattr(gdb, "driver", None)
    verify = getattr(driver, "verify_connectivity", None)
    if callable(verify):
        try:
            verify()
            return True, "图数据库可连接"
        except Exception as exc:
            return False, f"图数据库连接不可用：{exc}"
    if hasattr(gdb, "search_by_vector"):
        return True, "未提供连接探针，按已初始化处理"
    return False, "图数据库客户端不支持向量检索"


def _collect_input_files(state: AgentState) -> List[str]:
    """可检索的用户文件；改写目标本身不作为检索来源。"""
    rewrite_file = state.get("rewrite_file")
    rewrite_path = str(Path(rewrite_file).resolve()) if rewrite_file else ""
    files: List[str] = []
    for raw in state.get("input_file_path") or []:
        path = Path(str(raw))
        if not path.is_file():
            continue
        resolved = str(path.resolve())
        if rewrite_path and resolved.lower() == rewrite_path.lower():
            continue
        files.append(resolved)
    return files


def _check_capabilities(skill: DocumentSkill, state: AgentState, gdb: Any,
                        kg: Any = None, remote_kg_tool: Any = None) -> Dict[str, Any]:
    """检查 Skill 允许的检索来源、模板与权限是否真实可用。"""
    files = _collect_input_files(state)
    if "knowledge" not in skill.research.source.priority:
        knowledge_available, knowledge_detail = False, "Skill 未启用知识库来源"
    elif remote_kg_tool is not None:
        from kg_retrieve_node import _run_sync
        async def probe():
            try:
                return await remote_kg_tool.get_status(probe_llm=False)
            finally:
                closer = getattr(remote_kg_tool, "aclose", None)
                if callable(closer):
                    await closer()
        try:
            status = _run_sync(probe())
            knowledge_available = bool(status.get("ok"))
            knowledge_detail = "远程知识图谱可连接" if knowledge_available else "远程知识图谱未就绪"
            knowledge_detail += "；research.knowledge.* 仅适用于本地检索，远程参数由远程检索器控制"
        except Exception as exc:
            knowledge_available, knowledge_detail = False, f"远程知识图谱不可用：{exc}"
    else:
        knowledge_available, knowledge_detail = _probe_knowledge(gdb)
    if remote_kg_tool is None and kg is None and knowledge_available:
        knowledge_detail += "；知识图谱记忆未就绪，仅使用篇章摘要树"

    template_path = str(skill.template_path or "")
    if template_path:
        template_available = Path(template_path).is_file()
        template_detail = template_path if template_available else f"模板不存在：{template_path}"
    else:
        template_available = False
        template_detail = "未配置文档模板"

    return {
        "file": {
            "available": bool(files),
            "detail": f"可检索文件 {len(files)} 个" if files else "用户未提供可检索的参考文件",
            "files": files,
        },
        "knowledge": {
            "available": knowledge_available,
            "detail": knowledge_detail,
        },
        "template": {
            "available": template_available,
            "detail": template_detail,
        },
        "permissions": {
            "available": True,
            "detail": "本地进程运行，系统未实现权限模型",
        },
    }


def _plan_queries(llm, skill: DocumentSkill, user_text: str, capabilities: Dict[str, Any]) -> Optional[Dict[str, List[str]]]:
    """用模型把研究问题改写成检索式；失败时返回 None，由调用方使用默认查询。"""
    if llm is None or not (skill.research.questions or skill.research.data_fields):
        return None
    file_names = [Path(path).name for path in capabilities["file"]["files"]]
    prompt = build_plan_prompt(skill, user_text, file_names)
    draft = llm_model_invoke(llm, prompt, RetrievalPlanDraft)
    if draft is None:
        print("【检索计划】模型未能生成检索式，改用问题原文作为查询")
        return None
    # 模型可能把 id 写成小写，统一成大写后再与 Q1/Q2 对齐
    planned = {
        str(item.id).strip().upper(): list(item.queries)
        for item in draft.questions
        if item.queries
    }
    if planned:
        print(f"【检索计划】已生成 {len(planned)} 个问题的检索式")
    return planned or None


def create_skill_node(llm=None, gdb: Any = None, kg: Any = None, remote_kg_tool: Any = None):
    def skill_node(state: AgentState) -> AgentState:
        user_text = get_last_user_text(state.get("messages")).strip()
        explicit = str(state.get("skill_id") or "").strip()

        try:
            if state.get('intent_prepared'):
                skill, reason = coerce_skill(state.get('skill')), state.get('skill_reason', '')
            else:
                skill, reason = resolve_skill(user_text, explicit=explicit, llm_hint=state.get('skill_name'),
                                               use_skill=state.get('use_skill'), excluded_skills=state.get('excluded_skills') or [],
                                               task_type=state.get('user_intent') or 'new')
        except SkillError as exc:
            return {**state, 'state': 'error', 'error': str(exc)}
        print(f'【Skill 匹配】{reason}')
        if skill is None:
            return {**state, 'skill': None, 'skill_id': None}

        capabilities = _check_capabilities(skill, state, gdb, kg, remote_kg_tool)
        print(f"【Skill】已加载 {skill.summary()}｜匹配依据：{reason}")
        for name in ("file", "knowledge", "template"):
            item = capabilities[name]
            mark = "可用" if item["available"] else "不可用"
            print(f"    - {name}：{mark}｜{item['detail']}")

        available_sources = [
            source for source in skill.research.source.priority
            if capabilities[source]["available"]
        ]
        unavailable_sources = [
            source for source in skill.research.source.priority
            if not capabilities[source]["available"]
        ]
        requires_research = bool(skill.research.questions or skill.research.data_fields)
        if requires_research and not available_sources:
            details = "；".join(
                f"{source}：{capabilities[source]['detail']}" for source in unavailable_sources
            )
            message = f"Skill「{skill.name}」所需的检索来源均不可用：{details}"
            print(f"[Skill 能力错误] {message}")
            return {**state, "state": "error", "error": message}
        for source in unavailable_sources:
            print(f"【Skill 能力】{source} 不可用，本次跳过该来源：{capabilities[source]['detail']}")

        planned_queries = _plan_queries(llm, skill, user_text, capabilities)
        plan = build_retrieval_plan(
            skill,
            capabilities["file"]["files"],
            available_sources,
            rewrite_file=state.get("rewrite_file"),
            planned_queries=planned_queries,
        )
        if plan["questions"]:
            step_count = len(plan["steps"])
            print(
                f"【检索计划】{len(plan['questions'])} 个问题、{step_count} 个检索步骤；"
                f"来源：{'、'.join(available_sources) or '（无）'}；预算：{plan['max_rounds']} 次"
            )
            for question in plan["questions"]:
                print(f"    {question['id']}：{question['question']}｜查询：{' / '.join(question['queries'])}")

        return {
            **state,
            "skill": skill.to_state_dict(),
            "skill_id": skill.id,
            # 检索轮次预算由 Skill 接管，避免默认的 3 轮限制覆盖技能配置
            "retrieve_count_limit": skill.research.max_rounds,
            "capabilities": capabilities,
            "retrieval_plan": plan,
            "retrieval_progress": new_progress(),
            "retrieval_report": None,
            "citations": [],
            "unmet_requirements": [],
        }

    return skill_node


def route_after_skill(state: AgentState) -> str:
    """Skill 加载、适用性或能力校验失败时直接结束流程。"""
    if state.get("state") == "error":
        return "end"
    return "continue"
