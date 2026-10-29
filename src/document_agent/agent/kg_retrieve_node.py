"""知识图谱检索节点：把用户会话里的事实需求翻译成对远程知识图谱的一次只读检索。

它在 agent 里的位置是 ``intent → summary → retrieve →（knowledge）→ 本节点 → retrieve``：
``retrieve`` 节点用大模型决定"用哪个工具、查什么"（写入 ``retrieve_target`` / ``retrieve_params``），
本节点负责把这次查询真正落到知识图谱上，把证据写回 ``retrieved_content``。

检索过程由用户会话决定
----------------------
单个固定查询串无法覆盖"甲和乙什么关系""这位创始人的公司有哪些产品"这类需求，所以本节点
先让大模型结合**用户会话 + 检索目标 + 图谱概况**产出一份 :class:`KGSearchPlan`（关键词、
待定位实体、语义问句、展开跳数、读回几段原文），再按计划执行协议 §11 的最小闭环：

1. ``status``   自检，三项不全绿就报错退出（不消耗模型调用，``probe_llm=False``）
2. ``schema``   读图谱概况，作为规划上下文
3. ``keywords`` + ``semantic``  两路并发召回实体/关系/分块/文档
4. ``resolve``  把用户原话里的说法（含别名）对齐到图谱实体
5. ``traverse`` 从命中实体取 1~2 跳子图，补齐关系网与提及它的原文分块
6. ``paths``    两个实体之间查最短关系链（"他们怎么连上的"）
7. ``bodies``   批量读回分块原文作为写作证据

结果写入 ``retrieved_content``（list[str]，与其它检索工具的内容累加、按行去重），
每条都带 ``【实体】/【关系】/【分块】/【原文】/【路径】/【文档】`` 前缀，方便写作节点直接引用。
全过程只读，不修改图谱。

错误处理
--------
检索过程中出现任何异常（服务未就绪、Neo4j 不可用、鉴权失败、超时……）都**立即返回**，
把原因写进 ``error`` 字段并置 ``state="error"``，不返回半成品结果。
唯一的例外是"实体/端点不存在"（图谱里没有这个名字），按"没检索到"处理并跳过，
因为它不代表检索失败。

用法
----
.. code-block:: python

    from document_agent.retrieve_tool.remote_kg_tool import RemoteKGTool
    from kg_retrieve_node import create_kg_retrieve_node

    kg_tool = RemoteKGTool("http://127.0.0.1:8000")
    kg_node = create_kg_retrieve_node(llm, kg_tool)
    state = kg_node(state)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from agent_core import AgentState, get_last_user_text
from llm_client import llm_model_invoke
from document_agent.retrieve_tool.remote_kg_tool import (
    RemoteKGError,
    RemoteKGNotFoundError,
    RemoteKGTimeoutError,
    RemoteKGTool,
)

# ---- 结果规模与上下文预算（在协议 §2.4 的上限之内，按写作提示词的体量再收紧） ----
MAX_TOTAL_CHARS = 12000  # 本次检索写入 retrieved_content 的总字符预算
MAX_KEYWORD_QUERIES = 3  # 计划里最多采纳几条关键词检索串
KEYWORD_CHANNEL_LIMIT = 5  # 每条检索串在实体/分块/文档三个通道各自的条数
SEMANTIC_ENTITY_LIMIT = 8
SEMANTIC_RELATION_LIMIT = 8
MAX_TRAVERSE_STARTS = 3  # 最多从几个实体出发取子图
TRAVERSE_NODE_LIMIT = 80
TRAVERSE_EDGE_LIMIT = 150
PATH_MAX_DEPTH = 3
PATH_LIMIT = 2
MAX_ENTITIES = 12  # 结果里最多展示多少个实体
MAX_CHUNKS = 10  # 结果里最多展示多少个分块
MAX_DOCUMENTS = 3  # 结果里最多展示多少个文档
MAX_BODY_CHUNKS = 6  # 最多读回几段原文（计划里的 max_chunks 也在此区间内）
BODY_CHARS_RANGE = (200, 3000)
CONVERSATION_TURNS = 6  # 规划时回看最近几条消息
CONVERSATION_CHARS = 2000
RESOLVE_TEXT_MAX = 19000  # 协议 §10 的 text 上限是 20000，留一点余量
STEP_TIMEOUT = 35  # 单步超时：需 ≥ 客户端 timeout×(max_retries+1)，否则重试永远跑不完
NODE_TIMEOUT = 300  # 整轮上限：5 个受单步约束的步骤 + 2 路 HTTP 召回 + 一次计划模型调用

# 关系类型的中文标签，用于把路径/边渲染成人能读的样子
_EDGE_LABELS = {
    "HAS_CHUNK": "包含分块",
    "NEXT": "下一段",
    "MENTIONS": "提及",
    "RELATES": "相关",
}

# 这些"类型"只是图里的关系标签，不代表具体语义（真正的语义在 properties.type 里）
_GENERIC_RELATION_TYPES = frozenset({"", "相关", "关联", "RELATES"})

# 实体消歧的 match 字段（协议 §10）到中文的映射
_MATCH_LABELS = {
    "exact": "精确命中",
    "alias": "别名命中",
    "contains": "包含命中",
    "fuzzy": "模糊命中",
    "embedding": "向量命中",
}


class KGRetrievalError(RuntimeError):
    """检索流程内部的确定性失败（例如服务未就绪），其消息可直接写进 error 字段。"""


class KGSearchPlan(BaseModel):
    """知识图谱检索计划：由大模型根据用户会话决定"怎么查"，本地只做范围裁剪。"""

    focus: str = Field(
        default="",
        description="这次检索要拿到的核心信息，一句话概括（用于结果抬头与排查，不复述用户原话）。",
    )
    keywords: List[str] = Field(
        default_factory=list,
        description=(
            "2~3 条互补的关键词检索串。只写实体名、职能、术语、动作词，用空格分隔；"
            "不要整句、不要标点，例如 '张三 任职'、'安全生产 责任人'。"
        ),
    )
    entities: List[str] = Field(
        default_factory=list,
        description=(
            "需要从图谱里精确定位的实体名或别名（对话里出现的公司名、人名、产品名、部门名等）。"
            "拿不准就留空，由检索结果自行决定。"
        ),
    )
    entity_types: List[str] = Field(
        default_factory=list,
        description="限定实体类型，只能从给定的实体类型列表里选；不确定时留空表示不限类型。",
    )
    semantic_query: str = Field(
        default="",
        description="语义检索用的一句自然语言问句，例如 '谁负责安全生产管理'、'这家公司的创始人是谁'。",
    )
    traverse_depth: int = Field(
        default=1,
        description=(
            "命中实体的展开跳数：0=只看实体本身不展开；1=取一跳关系与原文证据（默认）；"
            "2=需要看两跳关系网（如'甲和乙是怎么联系上的'）时才用。"
        ),
    )
    max_chunks: int = Field(
        default=4,
        description="最多读回几段分块原文作为写作证据，1~6；只要事实不要原文时给 1~2。",
    )
    body_chars: int = Field(
        default=1500,
        description="每段原文的字符上限，200~3000；需要完整表述时给大一些。",
    )
    reason: str = Field(
        default="",
        description="简述检索思路：为什么选这些关键词/实体、为什么这样展开。",
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    """把模型给出的数字裁剪到合法区间；不是数字时用默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    return max(low, min(high, number))


def _clip(text: Any, limit: int) -> str:
    """压缩空白并截断到指定字数（超出补省略号）。"""
    content = " ".join(str(text or "").split())
    if len(content) <= limit:
        return content
    return content[: limit - 1].rstrip() + "…"


def _clean_list(values: Optional[Sequence[Any]], limit: int) -> List[str]:
    """去空白、去重、限量地规整字符串列表。"""
    result: List[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _schema_brief(schema: Dict[str, Any]) -> str:
    """把 /retrieval/schema 的响应压成几行给大模型看的图谱概况。"""
    labels = [
        f"{item.get('label')}({item.get('count')})"
        for item in (schema.get("labels") or [])
        if isinstance(item, dict) and item.get("label")
    ]
    relations = [
        f"{item.get('type')}({item.get('count')})"
        for item in (schema.get("relationship_types") or [])
        if isinstance(item, dict) and item.get("type")
    ]
    entity_types = [str(item) for item in (schema.get("entity_types") or []) if str(item).strip()]
    search = schema.get("search") or {}
    if search.get("embedding_enabled"):
        semantic_mode = f"向量精排已开启（{search.get('embedding_model') or '未标注模型'}）"
    else:
        semantic_mode = "仅字面相似度（lexical，同义改写不敏感）"
    return "\n".join(
        [
            f"- 节点：{'、'.join(labels) if labels else '（图谱为空）'}",
            f"- 关系：{'、'.join(relations) if relations else '（图谱为空）'}",
            f"- 实体类型：{'、'.join(entity_types) if entity_types else '（未配置）'}",
            f"- 语义检索：{semantic_mode}",
        ]
    )


def _role_of(message: Any) -> str:
    """兼容 dict 与 langchain 消息对象，取角色名。"""
    if isinstance(message, dict):
        role = str(message.get("role") or "user")
    else:
        role = str(getattr(message, "type", "") or getattr(message, "role", "") or "user")
    mapping = {"human": "用户", "ai": "助手", "system": "系统", "user": "用户", "assistant": "助手"}
    return mapping.get(role.lower(), role)


def _content_of(message: Any) -> str:
    """兼容 dict 与 langchain 消息对象，取正文。"""
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, list):  # 多模态消息：只取文本分片
        content = " ".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "").strip()


def _conversation_text(messages: Sequence[Any]) -> str:
    """把最近几条会话消息拼成规划上下文，避免把整段历史都塞给模型。"""
    turns: List[str] = []
    for message in list(messages or [])[-CONVERSATION_TURNS:]:
        content = _content_of(message)
        if content:
            turns.append(f"{_role_of(message)}：{_clip(content, 600)}")
    return _clip("\n".join(turns), CONVERSATION_CHARS) or "（无会话内容）"


# ---------------------------------------------------------------------------
# 规划：让大模型按用户会话决定检索过程
# ---------------------------------------------------------------------------


def build_kg_plan_prompt(
    *,
    target: str,
    conversation: str,
    schema_brief: str,
    entity_types: Sequence[str],
    user_intent: str,
) -> str:
    """构建检索规划提示词：说清图谱能查什么，再要求模型给出可执行的检索计划。"""
    if user_intent == "rewrite":
        task_mode = "改写已有文档（检索重点通常是参考依据、规范条款、可比数据）"
    else:
        task_mode = "全新撰写文档（检索重点是事实、定义、数据、流程）"
    types_hint = "、".join(entity_types) if entity_types else "（图谱未配置实体类型，留空即可）"
    return (
        "你是文档写作助手的知识图谱检索规划器。图谱是只读的，包含三类节点："
        "Document（文档）、Chunk（文档分块原文）、Entity（实体：人物/组织/地点/产品/事件/概念等）；"
        "关系有四类：HAS_CHUNK（文档→分块）、NEXT（分块→下一段）、MENTIONS（分块→实体）、"
        "RELATES（实体→实体，带关系类型与描述）。\n\n"
        f"【任务模式】{task_mode}\n\n"
        f"【用户会话】\n{conversation}\n\n"
        f"【本次检索目标】（上层规划给出的查询意图）\n{target}\n\n"
        f"【图谱概况】\n{schema_brief}\n\n"
        "【可用检索方式】\n"
        "1. keywords：关键词检索，按空格与中英文标点切词，分别在实体名、分块正文、文档标题里匹配；已知专有名词时最有效。\n"
        "2. semantic：语义检索，用一句自然语言描述要什么，返回相关实体与关系；不确定对方怎么措辞时用它。\n"
        "3. resolve：把用户原话里的说法（含别名）对齐到图谱里的实体。\n"
        "4. traverse：从实体出发取 k 跳子图，拿到它的关系网，以及提及它的原文分块。\n"
        "5. paths：查两个实体之间的最短关系链（谁和谁怎么连上的）。\n\n"
        "【规划要求】\n"
        f"1. keywords 给 2~{MAX_KEYWORD_QUERIES} 条互补的检索串，只写实体名/职能/术语/动作词，用空格分隔，不要整句、不要标点。\n"
        "2. entities 列出对话中需要精确落地的实体名或别名；没提到具体实体就留空，不要臆造。\n"
        f"3. entity_types 只能从这些类型里选：{types_hint}；不确定就留空表示不限。\n"
        "4. semantic_query 写一句自然语言问句；与 keywords 角度互补，不要照抄 keywords。\n"
        "5. traverse_depth：只要事实/原文证据填 1；只想知道实体本身填 0；"
        "需要看两跳关系网（如'甲和乙什么关系'）填 2。\n"
        "6. max_chunks 与 body_chars 控制读回多少原文证据：够写作用即可，不要贪多。\n"
        "7. focus 一行说明这次要拿到的信息；reason 简述思路。\n"
    )


def fallback_plan(target: str) -> KGSearchPlan:
    """大模型不可用或解析失败时的确定性兜底：直接拿检索目标做关键词与语义查询。"""
    return KGSearchPlan(
        focus=target,
        keywords=[target],
        entities=[],
        entity_types=[],
        semantic_query=target,
        traverse_depth=1,
        max_chunks=4,
        body_chars=1500,
        reason="大模型规划不可用，按检索目标直接做关键词+语义检索并展开一跳",
    )


def plan_search(
    llm: Any,
    *,
    target: str,
    conversation: str,
    schema: Dict[str, Any],
    user_intent: str,
) -> KGSearchPlan:
    """产出并规整检索计划；规划失败时退回兜底计划（不当作检索失败）。"""
    if llm is None:
        return fallback_plan(target)

    entity_types = [str(item) for item in (schema.get("entity_types") or []) if str(item).strip()]
    prompt = build_kg_plan_prompt(
        target=target,
        conversation=conversation,
        schema_brief=_schema_brief(schema),
        entity_types=entity_types,
        user_intent=user_intent,
    )
    plan = llm_model_invoke(llm, prompt, KGSearchPlan)
    if plan is None:
        print("【知识图谱检索】检索计划解析失败，改用确定性兜底计划")
        return fallback_plan(target)

    # 模型可能给出超限/空值，这里统一裁剪
    plan.focus = _clip(plan.focus or target, 200)
    plan.keywords = _clean_list(plan.keywords, MAX_KEYWORD_QUERIES)
    plan.entities = _clean_list(getattr(plan, "entities", None), 6)
    plan.entity_types = _clean_list(getattr(plan, "entity_types", None), 6)
    plan.semantic_query = str(plan.semantic_query or "").strip()
    plan.reason = _clip(plan.reason, 300)

    # 只保留图谱真有的实体类型，避免把不存在的类型传给服务端
    if entity_types and plan.entity_types:
        known = set(entity_types)
        kept = [item for item in plan.entity_types if item in known]
        if kept != plan.entity_types:
            dropped = [item for item in plan.entity_types if item not in known]
            print(f"【知识图谱检索】忽略图谱中不存在的实体类型：{dropped}")
        plan.entity_types = kept

    plan.traverse_depth = _clamp(plan.traverse_depth, 0, 2, 1)
    plan.max_chunks = _clamp(plan.max_chunks, 1, MAX_BODY_CHUNKS, 4)
    plan.body_chars = _clamp(plan.body_chars, BODY_CHARS_RANGE[0], BODY_CHARS_RANGE[1], 1500)
    if not plan.keywords and not plan.semantic_query:
        plan.keywords = [target]
        plan.semantic_query = target
    print(
        f"【知识图谱检索】计划：关键词={plan.keywords}；实体={plan.entities or '无'}；"
        f"语义问句={plan.semantic_query or '无'}；展开={plan.traverse_depth} 跳；"
        f"读回 {plan.max_chunks} 段/每段 {plan.body_chars} 字；思路：{plan.reason or '-'}"
    )
    return plan


# ---------------------------------------------------------------------------
# 结果归集：按 id 去重，保留首次出现的顺序（服务端已按 score 排好）
# ---------------------------------------------------------------------------


def _add_entity(bucket: Dict[str, Dict[str, Any]], item: Any) -> None:
    if not isinstance(item, dict):
        return
    node_id = str(item.get("id") or "").strip()
    if not node_id:
        return
    if node_id not in bucket:
        bucket[node_id] = dict(item)
        return
    # 同一实体可能被多路召回：保留信息更全的那一份
    current = bucket[node_id]
    for key in ("description", "type", "mentions", "aliases", "score"):
        if not current.get(key) and item.get(key):
            current[key] = item[key]


def _add_chunk(bucket: Dict[str, Dict[str, Any]], item: Any) -> None:
    if not isinstance(item, dict):
        return
    node_id = str(item.get("id") or "").strip()
    if node_id and node_id not in bucket:
        bucket[node_id] = dict(item)


def _add_document(bucket: Dict[str, Dict[str, Any]], item: Any) -> None:
    if not isinstance(item, dict):
        return
    node_id = str(item.get("id") or "").strip()
    if node_id and node_id not in bucket:
        bucket[node_id] = dict(item)


def _add_relation(bucket: Dict[str, Dict[str, Any]], rel: Dict[str, Any]) -> None:
    """关系按 (源, 类型, 目标) 去重，保留描述/权重更完整的一条。"""
    source = str(rel.get("source") or "").strip()
    target = str(rel.get("target") or "").strip()
    rel_type = str(rel.get("type") or "").strip()
    if not source or not target or not rel_type:
        return
    key = f"{source}|{rel_type}|{target}"
    if key not in bucket:
        bucket[key] = dict(rel)
        return
    current = bucket[key]
    for field in ("description", "weight", "score", "source_name", "target_name"):
        if not current.get(field) and rel.get(field):
            current[field] = rel[field]


def _drop_generic_relations(bucket: Dict[str, Dict[str, Any]]) -> None:
    """同一对实体既有"相关"（语义召回）又有具体关系（遍历给出）时，只留具体的。

    语义检索的 relations 只带关系标签（RELATES）与描述，给不出"创立/任职于"这种
    具体类型；遍历的 properties.type 才有。两条都留着会让结果重复、白占预算。
    """
    specific_pairs = {
        (str(item.get("source") or ""), str(item.get("target") or ""))
        for item in bucket.values()
        if str(item.get("type") or "") not in _GENERIC_RELATION_TYPES
    }
    drop = [
        key
        for key, item in bucket.items()
        if str(item.get("type") or "") in _GENERIC_RELATION_TYPES
        and (str(item.get("source") or ""), str(item.get("target") or "")) in specific_pairs
    ]
    for key in drop:
        bucket.pop(key, None)


def _collect_keyword_results(
    payload: Dict[str, Any],
    entities: Dict[str, Dict[str, Any]],
    chunks: Dict[str, Dict[str, Any]],
    documents: Dict[str, Dict[str, Any]],
) -> None:
    for item in payload.get("entities") or []:
        _add_entity(entities, item)
    for item in payload.get("chunks") or []:
        _add_chunk(chunks, item)
    for item in payload.get("documents") or []:
        _add_document(documents, item)


def _collect_semantic_results(
    payload: Dict[str, Any],
    entities: Dict[str, Dict[str, Any]],
    relations: Dict[str, Dict[str, Any]],
) -> None:
    for item in payload.get("entities") or []:
        _add_entity(entities, item)
    for item in payload.get("relations") or []:
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type") or "").strip()
        _add_relation(
            relations,
            {
                "source": item.get("source_id"),
                "target": item.get("target_id"),
                # 语义检索返回的是关系标签（RELATES），展示时换成中文"相关"；
                # 真正的中文关系名要等 traverse 的 properties.type 补上
                "type": _EDGE_LABELS.get(raw_type, raw_type),
                "source_name": item.get("source_name"),
                "target_name": item.get("target_name"),
                "description": item.get("description"),
                "weight": item.get("weight"),
                "score": item.get("score"),
            },
        )


def _collect_subgraph(
    payload: Dict[str, Any],
    entities: Dict[str, Dict[str, Any]],
    chunks: Dict[str, Dict[str, Any]],
    documents: Dict[str, Dict[str, Any]],
    relations: Dict[str, Dict[str, Any]],
    name_map: Dict[str, str],
) -> None:
    """把遍历得到的子图并入结果：实体/分块/文档按节点收，RELATES 边按关系收。"""
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        kind = str(node.get("kind") or "").lower()
        properties = node.get("properties") or {}
        name = str(node.get("name") or node_id)
        if node_id and name:
            name_map[node_id] = name
        if kind == "entity":
            _add_entity(
                entities,
                {
                    "id": node_id,
                    "name": name,
                    "type": node.get("type") or "",
                    "description": properties.get("description") or "",
                    "mentions": properties.get("mention_count"),
                    "aliases": properties.get("aliases") or [],
                },
            )
        elif kind == "chunk":
            _add_chunk(
                chunks,
                {
                    "id": node_id,
                    "document_title": properties.get("document_title") or "",
                    "document_id": properties.get("document_id") or "",
                    "chunk_index": properties.get("index"),
                },
            )
        elif kind == "document":
            _add_document(
                documents,
                {
                    "id": node_id,
                    "title": name,
                    "path": properties.get("path") or "",
                    "chunk_count": properties.get("chunk_count"),
                },
            )
    for edge in payload.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("type") or "") != "RELATES":  # 只有实体间的关系对写作有用
            continue
        properties = edge.get("properties") or {}
        _add_relation(
            relations,
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "type": properties.get("type") or "相关",
                "description": properties.get("description") or "",
                "weight": properties.get("weight"),
            },
        )


# ---------------------------------------------------------------------------
# 结果渲染：每行都以【类别】开头，写作节点可以直接引用
# ---------------------------------------------------------------------------


def _entity_label(entity: Dict[str, Any]) -> str:
    """实体名的展示文本，取不到名字时退回 id。"""
    return str(entity.get("name") or entity.get("id") or "").strip()


def _fmt_entity(entity: Dict[str, Any]) -> str:
    """【实体】名称（类型｜提及次数｜别名｜相关度）：描述"""
    name = _entity_label(entity)
    extras: List[str] = []
    entity_type = str(entity.get("type") or "").strip()
    if entity_type:
        extras.append(entity_type)
    mentions = entity.get("mentions")
    if isinstance(mentions, int) and not isinstance(mentions, bool) and mentions > 0:
        extras.append(f"提及 {mentions} 次")
    aliases = [str(item) for item in (entity.get("aliases") or []) if str(item).strip()]
    if aliases:
        extras.append("别名：" + "、".join(aliases[:4]))
    score = entity.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        extras.append(f"相关度 {float(score):.2f}")
    head = f"【实体】{name}" + (f"（{'｜'.join(extras)}）" if extras else "")
    description = _clip(entity.get("description"), 300)
    return f"{head}：{description}" if description else head


def _fmt_chunk(chunk: Dict[str, Any]) -> str:
    """【分块】《文档标题》第 N 段（id）：摘要或正文片段"""
    title = str(chunk.get("document_title") or "").strip()
    index = chunk.get("chunk_index", chunk.get("index"))
    location = f"《{title}》" if title else ""
    if isinstance(index, int) and not isinstance(index, bool):
        location += f"第 {index} 段"
    location = location or "分块"
    text = _clip(chunk.get("snippet") or chunk.get("text") or "", 300)
    head = f"【分块】{location}（{chunk.get('id')}）"
    return f"{head}：{text}" if text else head


def _fmt_document(document: Dict[str, Any]) -> str:
    """【文档】《标题》 路径（id，N 个分块）"""
    title = str(document.get("title") or document.get("id") or "").strip()
    path = str(document.get("path") or "").strip()
    count = document.get("chunk_count")
    count_text = ""
    if isinstance(count, int) and not isinstance(count, bool):
        count_text = f"，{count} 个分块"
    location = f" {path}" if path else ""
    return f"【文档】《{title}》{location}（{document.get('id')}{count_text}）"


def _fmt_relation(relation: Dict[str, Any], name_map: Dict[str, str]) -> str:
    """【关系】甲 —创立→ 乙：描述（权重）"""
    source = str(
        relation.get("source_name")
        or name_map.get(relation.get("source"))
        or relation.get("source")
        or ""
    )
    target = str(
        relation.get("target_name")
        or name_map.get(relation.get("target"))
        or relation.get("target")
        or ""
    )
    rel_type = str(relation.get("type") or "关联")
    description = _clip(relation.get("description"), 200)
    weight = relation.get("weight")
    weight_text = ""
    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
        weight_text = f"（权重 {float(weight):.2f}）"
    head = f"【关系】{source} —{rel_type}→ {target}"
    return f"{head}：{description}{weight_text}" if description else f"{head}{weight_text}"


def _fmt_body(payload: Dict[str, Any], name_map: Dict[str, str]) -> str:
    """【原文】《文档标题》第 N 段（id）：分块正文"""
    node = payload.get("node") or {}
    body = payload.get("body") or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return ""
    node_id = str(node.get("id") or "").strip()
    properties = node.get("properties") or {}
    title = str(properties.get("document_title") or "").strip()
    index = properties.get("index")
    location = f"《{title}》" if title else str(name_map.get(node_id) or "")
    if isinstance(index, int) and not isinstance(index, bool):
        location += f"第 {index} 段"
    location = location or "分块"
    truncated = "（已截断）" if body.get("truncated") else ""
    head = f"【原文】{location}（{node_id}）{truncated}"
    return f"{head}：{text}"


def _edge_label(edge: Dict[str, Any]) -> str:
    """边的展示名：RELATES 用属性里的中文关系类型，其余用内置中文标签。"""
    properties = edge.get("properties") or {}
    edge_type = str(edge.get("type") or "")
    if edge_type == "RELATES":
        return str(properties.get("type") or "相关")
    return _EDGE_LABELS.get(edge_type, edge_type or "相关")


def _fmt_path(path: Dict[str, Any], name_map: Dict[str, str]) -> str:
    """【路径】甲 --创立--> 乙 → 乙 --任职于--> 丙（共 N 跳）"""
    edges = [edge for edge in (path.get("edges") or []) if isinstance(edge, dict)]
    if not edges:
        return ""
    hops = [
        "{} --{}--> {}".format(
            name_map.get(edge.get("source")) or edge.get("source"),
            _edge_label(edge),
            name_map.get(edge.get("target")) or edge.get("target"),
        )
        for edge in edges
    ]
    return "【路径】" + " → ".join(hops) + f"（共 {len(edges)} 跳）"


def _fmt_resolved_mention(mention: Dict[str, Any]) -> str:
    """【实体提及】"老张" → 张三（别名命中，分数 1.00）"""
    text = str(mention.get("text") or "").strip()
    resolved = mention.get("resolved") or {}
    if not text or not isinstance(resolved, dict) or not resolved:
        return ""
    extras: List[str] = []
    match = str(resolved.get("match") or "")
    if match:
        extras.append(_MATCH_LABELS.get(match, match))
    score = resolved.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        extras.append(f"分数 {float(score):.2f}")
    suffix = f"（{'，'.join(extras)}）" if extras else ""
    return f"【实体提及】“{text}” → {resolved.get('name') or resolved.get('id')}{suffix}"


# ---------------------------------------------------------------------------
# 检索主流程（异步）：协议 §11 的最小闭环
# ---------------------------------------------------------------------------


def _components_error(status: Dict[str, Any]) -> str:
    """从 status 响应里挑出各组件错误，拼成一句可读原因。"""
    failures: List[str] = []
    for name, component in (status.get("components") or {}).items():
        if not isinstance(component, dict) or component.get("ok"):
            continue
        failures.append(f"{name}: {component.get('error') or '异常'}")
    return "；".join(failures) or "知识图谱服务未就绪（ok=false）"


async def _run_kg_search(
    kg: RemoteKGTool,
    llm: Any,
    *,
    target: str,
    conversation: str,
    user_text: str,
    user_intent: str,
    step_timeout: float = STEP_TIMEOUT,
) -> List[str]:
    """执行一次完整检索，返回待写入 retrieved_content 的文本行。

    正常情况下返回结果行；任何真正的失败都以异常抛出，由节点统一转成 error 字段。
    """
    lines: List[str] = []
    try:
        # 1. 调用前自检：三项全绿才继续（probe_llm=False 不消耗模型调用）
        status = await kg.get_status(probe_llm=False)
        if not status.get("ok"):
            raise KGRetrievalError(f"知识图谱服务未就绪，{_components_error(status)}")
        # 2. 图谱概况 → 交给大模型产出检索计划
        schema = await kg.get_schema()
        plan = plan_search(
            llm, target=target, conversation=conversation, schema=schema, user_intent=user_intent
        )

        entities: Dict[str, Dict[str, Any]] = {}
        chunks: Dict[str, Dict[str, Any]] = {}
        documents: Dict[str, Dict[str, Any]] = {}
        relations: Dict[str, Dict[str, Any]] = {}
        name_map: Dict[str, str] = {}

        # 3. 关键词 + 语义并发召回
        semantic_query = plan.semantic_query or target
        tasks = [
            kg.semantic_search(
                semantic_query,
                entity_limit=SEMANTIC_ENTITY_LIMIT,
                relation_limit=SEMANTIC_RELATION_LIMIT,
                min_score=0.0,
                entity_types=plan.entity_types or None,
            )
        ]
        tasks += [
            kg.keyword_search(query, kinds=None, limit=KEYWORD_CHANNEL_LIMIT)
            for query in plan.keywords
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 先等所有并发请求收尾，再抛出第一个失败：避免关连接池时还有请求在飞
        for item in results:
            if isinstance(item, BaseException):
                raise item
        semantic_payload = results[0] if results else {}
        if isinstance(semantic_payload, dict):
            _collect_semantic_results(semantic_payload, entities, relations)
        for keyword_payload in results[1:]:
            if isinstance(keyword_payload, dict):
                _collect_keyword_results(keyword_payload, entities, chunks, documents)

        # 4. 用户原话里的说法对齐到图谱实体（use_llm=False：纯词典匹配，省一次模型调用）
        resolve_text = (user_text or target)[:RESOLVE_TEXT_MAX]
        if resolve_text:
            resolved = await kg.resolve_entities(
                resolve_text,
                use_llm=False,
                top_k=3,
                min_score=0.55,
                entity_types=plan.entity_types or None,
            )
            for mention in resolved.get("mentions") or []:
                if not isinstance(mention, dict):
                    continue
                mention_line = _fmt_resolved_mention(mention)
                if mention_line:
                    lines.append(mention_line)
                resolved_entity = mention.get("resolved")
                if isinstance(resolved_entity, dict) and resolved_entity.get("id"):
                    _add_entity(entities, resolved_entity)

        # 5. 从计划实体与命中实体出发取子图（0 跳时不展开）
        if plan.traverse_depth > 0:
            starts: List[str] = []
            for name in plan.entities:
                if name not in starts:
                    starts.append(name)
            for entity in list(entities.values())[:MAX_TRAVERSE_STARTS]:
                entity_id = str(entity.get("id") or "").strip()
                if entity_id and entity_id not in starts:
                    starts.append(entity_id)
            for start in starts[:MAX_TRAVERSE_STARTS]:
                try:
                    subgraph = await asyncio.wait_for(
                        kg.traverse(
                            start,
                            start_kind="auto",
                            depth=plan.traverse_depth,
                            direction="both",
                            node_limit=TRAVERSE_NODE_LIMIT,
                            edge_limit=TRAVERSE_EDGE_LIMIT,
                        ),
                        timeout=step_timeout,
                    )
                except RemoteKGNotFoundError:
                    # 名字不在图谱里是正常情况（模型可能猜了名字），不算检索失败
                    print(f"【知识图谱检索】跳过图谱中不存在的起点：{start}")
                    continue
                except (asyncio.TimeoutError, RemoteKGTimeoutError) as exc:
                    # 单步超时不值得让整轮检索失败：跳过，已拿到的结果继续用
                    print(f"【知识图谱检索】起点 {start} 展开超时，已跳过：{exc}")
                    continue
                _collect_subgraph(subgraph, entities, chunks, documents, relations, name_map)

        # 6. 两个以上实体时查最短关系链
        entity_ids = [str(item.get("id")) for item in entities.values() if item.get("id")]
        if len(entity_ids) >= 2:
            try:
                found_paths = await asyncio.wait_for(
                    kg.find_paths(
                        entity_ids[0], entity_ids[1], max_depth=PATH_MAX_DEPTH, limit=PATH_LIMIT
                    ),
                    timeout=step_timeout,
                )
                for path in found_paths.get("paths") or []:
                    if not isinstance(path, dict):
                        continue
                    name_map.update(
                        {
                            str(node.get("id")): str(node.get("name") or node.get("id"))
                            for node in (path.get("nodes") or [])
                            if isinstance(node, dict) and node.get("id")
                        }
                    )
                    path_line = _fmt_path(path, name_map)
                    if path_line:
                        lines.append(path_line)
            except RemoteKGNotFoundError:
                print("【知识图谱检索】两个实体之间无可用端点，跳过路径查找")
            except (asyncio.TimeoutError, RemoteKGTimeoutError) as exc:
                print(f"【知识图谱检索】路径查找超时，已跳过：{exc}")

        # 7. 批量读回分块原文作为证据
        chunk_ids = [str(item.get("id")) for item in chunks.values() if item.get("id")]
        if plan.max_chunks > 0 and chunk_ids:
            try:
                bodies = await asyncio.wait_for(
                    kg.get_nodes_bodies(
                        chunk_ids[: plan.max_chunks], max_chars=plan.body_chars
                    ),
                    timeout=step_timeout,
                )
            except (asyncio.TimeoutError, RemoteKGTimeoutError) as exc:
                print(f"【知识图谱检索】读回原文超时，已跳过：{exc}")
                bodies = {}
            for payload in bodies.get("nodes") or []:
                if isinstance(payload, dict):
                    body_line = _fmt_body(payload, name_map)
                    if body_line:
                        lines.append(body_line)

        # 语义召回只有"相关"这类泛化类型，遍历才给出具体关系：去重后再渲染
        _drop_generic_relations(relations)

        # 8. 渲染实体/关系/分块/文档（原文之前，便于写作模型先看结论再看证据）
        document_lines = [_fmt_document(item) for item in list(documents.values())[:MAX_DOCUMENTS]]
        entity_lines = [_fmt_entity(item) for item in list(entities.values())[:MAX_ENTITIES]]
        relation_lines = [
            _fmt_relation(item, name_map)
            for item in list(relations.values())[:MAX_ENTITIES]
        ]
        chunk_lines = [_fmt_chunk(item) for item in list(chunks.values())[:MAX_CHUNKS]]
        lines = document_lines + entity_lines + relation_lines + chunk_lines + [line for line in lines if line]

        semantic_mode = "-"
        if isinstance(semantic_payload, dict):
            semantic_mode = str(semantic_payload.get("mode") or "-")
        header = (
            f"【知识图谱检索】目标：{target}｜焦点：{plan.focus}｜"
            f"实体 {len(entities)} 个、关系 {len(relations)} 条、分块 {len(chunks)} 个、"
            f"文档 {len(documents)} 个｜语义模式 {semantic_mode}"
        )
        lines.insert(0, header)

        # 三路召回全空时明确告诉上层"图里没有"，避免写作模型把空结果当成"没查"
        if not (entities or chunks or documents or relations):
            lines.append(f"【知识图谱】未在知识图谱中检索到与“{target}”相关的内容。")

        # 9. 按总字符预算裁剪，避免把写作提示词撑爆
        budgeted: List[str] = []
        used = 0
        for line in lines:
            if used + len(line) > MAX_TOTAL_CHARS:
                budgeted.append("……（受上下文长度限制，其余检索结果已省略）")
                break
            budgeted.append(line)
            used += len(line)
        return budgeted
    finally:
        # 节点每次执行都会新建连接池（事件循环隔离），退出时务必释放
        closer = getattr(kg, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except Exception as exc:  # 关闭失败不应覆盖检索结果
                print(f"【知识图谱检索】连接释放失败：{exc}")


def _run_sync(coroutine: Any) -> Any:
    """在同步节点里执行协程。

    没有运行中的事件循环时直接 ``asyncio.run``；已经处在事件循环里（例如上层用
    ``graph.ainvoke``）时放到独立线程的新循环里跑，避免 "coroutine was never awaited"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _merge_content(existing: Sequence[str], incoming: Sequence[str]) -> List[str]:
    """把新结果并入已有 retrieved_content，按行去重且保持原有顺序。"""
    merged: List[str] = [str(item) for item in (existing or [])]
    seen = set(merged)
    for line in incoming:
        text = str(line)
        if text and text not in seen:
            merged.append(text)
            seen.add(text)
    return merged


def _fail(state: AgentState, error: str, *, fatal: bool = False) -> AgentState:
    """远程图谱失败时的统一出口。

    三件事缺一不可：

    * ``retrieve_tool`` 置 ``end``：保持状态自洽，但**它并不阻止下一轮重新规划**——
      ``retrieve`` 节点只看 ``retrieve_count_limit`` 就无条件让模型重新决策，所以
      "不再白跑"真正靠的是：写进素材的不可用说明（模型能看到）+ 次数上限；
    * 原因写进 ``retrieved_content``：写作节点读的是这个字段，``error`` 会被后面的
      写作节点覆盖成 ``succeeded``，指望不上；
    * ``state`` 标记失败：``fatal=False`` 时是 ``error``（这一步失败、流程继续写）；
      ``fatal=True`` 时是 ``aborted``（按配置停写，路由直接结束、不生成文档）。

    注意：``fatal`` 只用于**故障类**失败（连不上、超时、401/503、没配客户端）；
    "查了但图里没有"不走 ``_fail``，那条路径继续降级写作——没查到不等于出错。
    """
    print(f"[知识图谱检索错误] {error}")
    merged = _merge_content(
        state.get("retrieved_content") or [],
        [f"【知识图谱不可用】{error}"],
    )
    return {
        **state,
        "state": "aborted" if fatal else "error",
        "error": error,
        "retrieve_tool": "end",
        "retrieved_content": merged,
    }


def create_kg_retrieve_node(
    llm: Any,
    kg_tool: Optional[RemoteKGTool],
    on_failure: str = "degrade",
    step_timeout: float = STEP_TIMEOUT,
    node_timeout: float = NODE_TIMEOUT,
):
    """创建知识图谱检索节点。

    :param llm: 用于规划检索过程的对话模型；传 ``None`` 时走确定性兜底计划
        （直接拿 ``retrieve_target`` 做关键词 + 语义检索并展开一跳）
    :param kg_tool: 远程知识图谱客户端实例（:class:`RemoteKGTool`），
        协议见 ``retrieve_protocol.md``；``None`` 时节点直接报错
    :param on_failure: 故障类失败后的策略。``degrade``（默认）继续写作并在素材里注明；
        ``stop`` 直接终止本次写作（用户明确要求"必须依据图谱"时应选它）
    :param step_timeout: 单步（遍历/路径/读原文）超时秒数，超了跳过并标注"部分来源超时"
    :param node_timeout: 整轮超时秒数（含一次检索计划模型调用）
    :return: 节点函数 ``kg_retrive(state) -> state``
    """
    fatal = str(on_failure or "").strip().lower() == "stop"

    def kg_retrive(state: AgentState) -> AgentState:
        messages = list(state.get("messages") or [])
        params = dict(state.get("retrieve_params") or {})
        user_text = get_last_user_text(messages).strip()

        # 检索目标优先用上层规划给出的 retrieve_target，其次取工具调用参数与用户原话
        target = (state.get("retrieve_target") or "").strip()
        if not target:
            target = str(params.get("query") or "").strip()
        if not target:
            target = user_text
        if not target:
            error = "知识图谱检索失败：缺少检索目标（retrieve_target、retrieve_params 与用户输入均为空）"
            return _fail(state, error, fatal=fatal)
        if kg_tool is None:
            error = "知识图谱检索失败：未配置远程知识图谱客户端（create_kg_retrieve_node 的第二个参数为空）"
            return _fail(state, error, fatal=fatal)

        print("=" * 20, "开始知识图谱检索，目标：", target, "=" * 20)
        conversation = _conversation_text(messages)
        try:
            lines = _run_sync(
                # 整轮总预算：远端卡住时最多等 NODE_TIMEOUT 秒，不会一直挂着
                asyncio.wait_for(
                    _run_kg_search(
                        kg_tool,
                        llm,
                        target=target,
                        conversation=conversation,
                        user_text=user_text,
                        user_intent=str(state.get("user_intent") or "new"),
                        step_timeout=step_timeout,
                    ),
                    timeout=node_timeout,
                )
            )
        except asyncio.TimeoutError:
            return _fail(state, f"知识图谱检索失败：超过 {node_timeout} 秒未完成，已放弃本轮远程检索", fatal=fatal)
        except (RemoteKGError, KGRetrievalError) as exc:
            return _fail(state, f"知识图谱检索失败：{exc}", fatal=fatal)
        except Exception as exc:  # 编码问题、事件循环问题等非协议错误同样直接返回
            return _fail(state, f"知识图谱检索失败：{type(exc).__name__}: {exc}", fatal=fatal)

        if not lines:
            lines = [f"【知识图谱】未在知识图谱中检索到与“{target}”相关的内容。"]

        merged = _merge_content(state.get("retrieved_content") or [], lines)
        print(f"【知识图谱检索】新增 {len(lines)} 条结果，累计 {len(merged)} 条")
        return {**state, "retrieved_content": merged}

    return kg_retrive
