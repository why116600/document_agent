"""检索计划与进度推进的纯函数。

把 Skill 配置编译成一条有序的检索队列：每个研究问题按「来源优先级 + 文件」展开，
再按尝试次数轮转，先保障各题首次检索。进度推进只依赖传入数据，便于单测。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from .schema import DocumentSkill


class PlannedQuestion(BaseModel):
    id: str = Field(description="问题编号，必须与输入保持一致，如 Q1")
    queries: List[str] = Field(
        default_factory=list, min_length=1, max_length=3, description="该问题的 1~3 条检索式"
    )


class RetrievalPlanDraft(BaseModel):
    questions: List[PlannedQuestion] = Field(default_factory=list)


def default_query(question: str, data_fields: Optional[Sequence[str]] = None) -> str:
    fields = [field for field in (data_fields or []) if field]
    if fields:
        return f"{question}（重点数据：{'、'.join(fields)}）"
    return question


def build_plan_prompt(skill: DocumentSkill, user_request: str, file_names: Sequence[str]) -> str:
    """让模型把研究问题改写成可执行的检索式（失败时由调用方退回默认查询）。"""
    question_lines = "\n".join(
        f"- {qid}：{question}"
        for qid, question in _question_items(skill)
    )
    files_desc = "、".join(file_names) if file_names else "（无用户文件）"
    return (
        "你是文档写作的检索计划助手。请为每个研究问题生成 1~3 条适合检索的查询式。\n"
        "查询式应包含主题词、指标名与必要的限定词（时间/地域），不要写成完整问句，不要编造数据。\n\n"
        f"【用户需求】\n{user_request}\n\n"
        f"【研究问题】\n{question_lines}\n\n"
        f"【必须收集的数据字段】{'、'.join(skill.research.data_fields) or '（无）'}\n"
        f"【时间范围】{skill.research.time_range or '（未限定）'}\n"
        f"【地域范围】{skill.research.region or '（未限定）'}\n"
        f"【可用来源】{'、'.join(skill.research.source.priority)}\n"
        f"【用户文件】{files_desc}\n\n"
        "输出要求：保持每个问题的 id 不变；每个问题至少 1 条、最多 3 条查询式。"
    )


def _question_items(skill: DocumentSkill) -> List[tuple]:
    items = [(f"Q{index}", question) for index, question in enumerate(skill.research.questions, 1)]
    if not items and skill.research.data_fields:
        items = [
            ("Q1", "收集以下数据字段：" + "、".join(skill.research.data_fields)),
        ]
    return items


def build_retrieval_plan(
    skill: DocumentSkill,
    input_files: Sequence[str],
    available_sources: Sequence[str],
    rewrite_file: Optional[str] = None,
    planned_queries: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """生成可顺序执行的检索计划。

    - 问题来自 research.questions；无问题但有 data_fields 时生成一条数据收集任务；
    - 每个问题按来源优先级展开；file 来源会为每个候选文件生成一步；
    - 单题最多 `per_question_attempts` 步，超出部分丢弃。
    """
    questions: List[Dict[str, Any]] = []
    sources = [source for source in skill.research.source.priority if source in available_sources]

    for qid, question in _question_items(skill):
        queries = list((planned_queries or {}).get(qid) or [])
        queries = [query.strip() for query in queries if str(query).strip()]
        if not queries:
            queries = [default_query(question, skill.research.data_fields)]
        questions.append({
            "id": qid,
            "question": question,
            "queries": queries[:3] if skill.research.query_expansion else queries[:1],
        })

    files = [
        str(path)
        for path in input_files or []
        if str(path).strip() and (not rewrite_file or str(path) != str(rewrite_file))
    ]

    question_steps: List[List[Dict[str, Any]]] = []
    limit = skill.research.per_question_attempts
    for question in questions:
        qsteps: List[Dict[str, Any]] = []
        for attempt in range(limit):
            query = question["queries"][min(attempt, len(question["queries"]) - 1)]
            for source in sources:
                if source == "knowledge":
                    qsteps.append({
                        "question_id": question["id"],
                        "question": question["question"],
                        "source": "knowledge",
                        "query": query,
                        "path": "",
                    })
                elif source == "file":
                    for path in files:
                        qsteps.append({
                            "question_id": question["id"],
                            "question": question["question"],
                            "source": "file",
                            "query": query,
                            "path": path,
                        })
            if len(qsteps) >= limit:
                break
        question_steps.append(qsteps[:limit])

    # 先让每个问题获得一次机会，再安排第二次尝试，避免早期问题耗尽全局预算。
    steps = [qsteps[attempt] for attempt in range(limit)
             for qsteps in question_steps if attempt < len(qsteps)]

    for index, step in enumerate(steps):
        step["index"] = index

    return {
        "skill_id": skill.id,
        "questions": questions,
        "sources": sources,
        "files": files,
        "steps": steps,
        "max_rounds": skill.research.max_rounds,
        "per_question_attempts": limit,
        "on_empty": skill.research.on_empty,
        "query_expansion": skill.research.query_expansion,
        "stop_after_no_new_evidence": skill.research.stop_after_no_new_evidence,
    }


def new_progress() -> Dict[str, Any]:
    return {
        "step_index": 0,
        "pending": None,
        "covered": [],
        "missing": [],
        "attempts": [],
        "overrides": {},
        "consecutive_empty": 0,
        "stop_reason": "",
    }


def current_step(plan: Dict[str, Any], progress: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    steps = plan.get("steps") or []
    index = int(progress.get("step_index") or 0)
    if index < 0 or index >= len(steps):
        return None
    return (progress.get("overrides") or {}).get(str(index), steps[index])


def begin_step(progress: Dict[str, Any], plan: Dict[str, Any], evidence_start: int) -> Dict[str, Any]:
    """把下一步标记为「等待评估」，并记录本轮开始前的证据条数。"""
    step = current_step(plan, progress)
    updated = dict(progress)
    if step is None:
        updated["pending"] = None
        return updated
    updated["pending"] = {"index": step["index"], "step": step, "evidence_start": evidence_start}
    return updated


def record_assessment(
    plan: Dict[str, Any],
    progress: Dict[str, Any],
    covered: bool,
    evidence_count: int,
    retry_query: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """记录上一轮检索是否回答当前问题，并推进到下一步。"""
    pending = progress.get("pending")
    if not pending:
        return progress

    steps = plan.get("steps") or []
    step = pending["step"]
    question_id = step["question_id"]
    covered_list = list(progress.get("covered") or [])
    missing_list = list(progress.get("missing") or [])
    attempts = list(progress.get("attempts") or [])
    attempts.append({
        "question_id": question_id,
        "source": step["source"],
        "query": step["query"],
        "path": step["path"],
        "evidence_count": evidence_count,
        "covered": covered,
        "reason": reason,
    })

    next_index = int(pending["index"]) + 1
    overrides = dict(progress.get("overrides") or {})
    empty_streak = 0 if evidence_count else int(progress.get("consecutive_empty") or 0) + 1
    ignored = set(progress.get("ignored") or [])
    question_indices = [index for index in range(next_index, len(steps))
                        if steps[index]["question_id"] == question_id and index not in ignored]
    if covered:
        if question_id not in covered_list:
            covered_list.append(question_id)
    else:
        if not evidence_count:
            policy = plan.get("on_empty", "rewrite_query")
            if policy == "mark_missing":
                if question_id not in missing_list:
                    missing_list.append(question_id)
            elif policy == "fallback_source":
                # 一个文件也是一个独立来源；跳过同一来源的重复尝试。
                ignored.update(index for index in question_indices if (
                    steps[index]["source"], steps[index]["path"]
                ) == (step["source"], step["path"]))
        if plan.get("query_expansion", True) and retry_query and question_indices:
            retry_index = question_indices[0]
            overrides[str(retry_index)] = {
                **steps[retry_index], "source": step["source"], "path": step["path"],
                "query": retry_query,
            }

    tried = {(item["question_id"], item["source"], item["path"], item["query"]) for item in attempts}
    eligible = []
    for index in range(next_index, len(steps)):
        candidate = overrides.get(str(index), steps[index])
        qid = candidate["question_id"]
        if index in ignored or qid in covered_list or qid in missing_list:
            continue
        if (qid, candidate["source"], candidate["path"], candidate["query"]) in tried:
            ignored.add(index)
            continue
        eligible.append(index)
    if not covered and not any(steps[index]["question_id"] == question_id for index in eligible):
        if question_id not in missing_list:
            missing_list.append(question_id)
    next_index = eligible[0] if eligible else len(steps)

    return {
        **progress,
        "step_index": next_index,
        "pending": None,
        "covered": covered_list,
        "missing": missing_list,
        "attempts": attempts,
        "overrides": overrides,
        "ignored": sorted(ignored),
        "consecutive_empty": empty_streak,
        "stop_reason": (
            f"连续 {empty_streak} 轮没有新增证据"
            if plan.get("stop_after_no_new_evidence") and empty_streak >= plan["stop_after_no_new_evidence"]
            else progress.get("stop_reason", "")
        ),
    }


def finalize_report(
    plan: Dict[str, Any],
    progress: Dict[str, Any],
    budget_total: int,
    budget_used: int,
) -> Dict[str, Any]:
    """生成检索报告：每个问题是否覆盖、尝试过的来源与缺失项。"""
    covered = list(progress.get("covered") or [])
    missing = list(progress.get("missing") or [])
    attempts = list(progress.get("attempts") or [])

    question_reports = []
    for question in plan.get("questions") or []:
        qid = question["id"]
        if qid not in covered and qid not in missing:
            # 预算耗尽或流程提前结束时，未确认覆盖的问题一律记为缺失
            missing.append(qid)
        q_attempts = [item for item in attempts if item["question_id"] == qid]
        question_reports.append({
            "id": qid,
            "question": question["question"],
            "covered": qid in covered,
            "attempts": len(q_attempts),
            "sources": sorted({item["source"] for item in q_attempts}),
        })

    return {
        "skill_id": plan.get("skill_id"),
        "sources": plan.get("sources") or [],
        "covered": covered,
        "missing": missing,
        "questions": question_reports,
        "budget": {"total": budget_total, "used": budget_used},
        "complete": not missing,
        "stop_reason": progress.get("stop_reason") or (
            "budget_exhausted" if budget_used >= budget_total else "plan_exhausted"
        ),
        "stop_evaluation": progress.get("stop_evaluation"),
        "attempts": attempts,
    }
