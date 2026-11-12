
from typing import Dict, List, Tuple
from datetime import datetime
from pathlib import Path

from llm_client import llm_invoke
from agent_core import AgentState
from retrieval_material import select_source_quote
from document_agent.retrieve_tool.extract_document import extract_document_content
from document_agent.skills.evidence import (
    EXTRACTION_VERSION,
    ensure_evidence_ids,
    file_digest,
    locate_paragraph,
    locate_quote,
)
from document_agent.skills.schema import coerce_skill

MAX_FILE_RETRIEVE_CHARS = 2000  # 单次文件检索结果上限，防止提示词被单段撑爆
MAX_CACHED_FILES = 64
_CONTENT_CACHE: Dict[Tuple[str, int, int], str] = {}


def _contains_normalized(text: str, needle: str) -> bool:
    """忽略空白后判断 needle 是否已在 text 中，避免同一原文摘录重复注入。"""
    def _norm(value):
        return "".join(str(value or "").split())
    return bool(needle) and _norm(needle) in _norm(text)


def _read_content(path: str) -> str:
    """按 (路径, 大小, mtime) 缓存提取结果，避免每个研究问题重复解析同一文档。"""
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return ""
    key = (str(target.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _CONTENT_CACHE.get(key)
    if cached is not None:
        return cached
    content = extract_document_content(path) or ""
    if len(_CONTENT_CACHE) >= MAX_CACHED_FILES:
        _CONTENT_CACHE.pop(next(iter(_CONTENT_CACHE)))
    _CONTENT_CACHE[key] = content
    return content


def _query_tokens(query: str) -> List[str]:
    """中文按二元字组切分，英文数字按词切分，用于确定性相关度打分。"""
    normalized = "".join(str(query or "").split()).lower()
    if not normalized:
        return []
    tokens = {normalized[index:index + 2] for index in range(max(1, len(normalized) - 1))}
    return [token for token in tokens if token]


def _score(text: str, query: str) -> int:
    haystack = "".join(str(text or "").split()).lower()
    if not haystack:
        return 0
    return sum(haystack.count(token) for token in _query_tokens(query))


def _split_chunks(text: str, size: int) -> List[str]:
    """按行聚合到目标字符数，超长单行按窗口切分。"""
    chunks: List[str] = []
    current: List[str] = []
    length = 0
    for line in str(text or "").split("\n"):
        line = line.rstrip()
        if len(line) > size:
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.extend(line[start:start + size] for start in range(0, len(line), size))
            continue
        if current and length + len(line) + 1 > size:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _select_chunks(content: str, query: str, spec) -> str:
    """按相关度挑选分块；短文档直接返回全文。"""
    text = str(content or "")
    budget = max(1, spec.top_k_chunks) * max(200, spec.chunk_chars)
    if len(text) <= budget:
        return text
    chunks = _split_chunks(text, spec.chunk_chars)
    if len(chunks) <= spec.top_k_chunks:
        return "\n…\n".join(chunks)
    scored = sorted(
        ((-_score(chunk, query), index, chunk) for index, chunk in enumerate(chunks)),
        key=lambda item: (item[0], item[1]),
    )
    chosen = sorted(scored[:spec.top_k_chunks], key=lambda item: item[1])
    return "\n…\n".join(item[2] for item in chosen)


def _rank_candidates(paths: List[str], query: str, cap: int) -> Tuple[List[str], List[Dict[str, str]]]:
    """候选文件超过上限时按内容相关度取前 N；排序与输入顺序无关。"""
    if cap <= 0 or len(paths) <= cap:
        return list(paths), []
    scored = sorted(
        ((-_score(_read_content(path), query), str(path), path) for path in paths),
        key=lambda item: (item[0], item[1]),
    )
    keep = {item[2] for item in scored[:cap]}
    selected = [path for path in paths if path in keep]
    skipped = [
        {"path": str(path), "reason": "max_files_per_attempt"}
        for path in paths if path not in keep
    ]
    return selected, skipped


# 文件检索节点

def create_file_retrieve_node(llm):
    def file_retrive(state : AgentState) -> AgentState:
        params=state["retrieve_params"] or {}
        retrieve_target=state["retrieve_target"]
        file_retrieved_items=dict(state.get("file_retrived_items") or {})
        retrieved_items=list(state.get("retrieved_content") or [])
        skill=coerce_skill(state.get("skill"))
        if not params.get("query"):
            return {**state,"state":"error","error":"文件检索缺少查询内容"}
        query=params.get("query")
        candidates=[str(item) for item in (params.get("paths") or []) if str(item).strip()]
        if not candidates and params.get("path"):
            candidates=[str(params["path"])]
        if not candidates:
            return {**state,"state":"error","error":"文件检索缺少必要参数字段：paths"}

        budget=params.get("budget") or {}
        max_tool_calls=int(budget.get("max_tool_calls") or 0)
        max_context=int(budget.get("max_context_chars") or 0)
        max_files=int(getattr(skill.research.file, "max_files_per_attempt", 0)) if skill else 0
        selected_paths, skipped = _rank_candidates(candidates, query, max_files)

        calls_made=0
        context_used=0
        evidence_records=list(state.get("evidence_records") or [])
        for path in selected_paths:
            if max_tool_calls and calls_made >= max_tool_calls:
                skipped.append({"path": path, "reason": "tool_call_budget"})
                continue
            cached_result=file_retrieved_items.get((path,query))
            if cached_result is not None and skill is None:
                retrieved_items.append(f"重复对文件{path}检索：{query}")
                continue
            content=_read_content(path)
            if not content:
                skipped.append({"path": path, "reason": "unreadable_or_unsupported"})
                print(f"【文件检索】跳过无法读取的文件：{path}")
                continue

            spec=skill.research.file if skill else None
            if spec is not None:
                excerpt=_select_chunks(content, query, spec)
            else:
                excerpt=content[:8000]
            if spec is not None and spec.max_chars and len(excerpt) > spec.max_chars:
                excerpt=excerpt[:spec.max_chars]+"\n…（内容超长，已截断）"
            if max_context and context_used + len(excerpt) > max_context:
                skipped.append({"path": path, "reason": "context_budget"})
                continue

            calls_made+=1
            context_used+=len(excerpt)
            print(f"【文件检索】{Path(path).name}｜候选片段 {len(excerpt)} 字")

            quote_body=""
            if cached_result is not None:
                # 不同研究问题可使用相同查询；复用提取结果，再按当前问题评估覆盖。
                result=cached_result
            else:
                prompt_for_retrieval=(
                    "你是一个文档检索助手，你的任务是根据用户提供的参考内容和用户对话检索出符合目标的内容。\n"
                    f"用户提供的文件内容（相关片段）：{excerpt}\n"
                    f"要检索的内容：{query}\n"
                    "请按以下格式输出，且只输出这两部分：\n"
                    "【原文摘录】逐句抄录与检索目标直接相关的原文句子；必须连同紧邻的限定条件、起算点、"
                    "适用范围、否定关系和例外一并保留；不要只摘第一句，不要改写、合并或省略限定语。\n"
                    "【检索回答】用简短文字回答检索目标，只能使用原文摘录中的事实，不得补充原文没有的信息。"
                )
                res=llm_invoke(llm, prompt_for_retrieval)
                raw=str(getattr(res,"content","") or "").strip()
                answer=raw.replace("【检索回答】","").strip()
                # 模型回答可能只摘第一句；这里用原文摘录兜底，确保紧邻限定句进入素材。
                quote=select_source_quote(content,query)
                quote_body=quote.replace("【原文摘录】","").strip() if quote else ""
                parts=[]
                if quote and not _contains_normalized(answer,quote_body):
                    parts.append(quote)
                if answer:
                    parts.append("【检索回答】\n"+answer)
                result="\n".join(parts).strip()
                if len(result)>MAX_FILE_RETRIEVE_CHARS:
                    result=result[:MAX_FILE_RETRIEVE_CHARS]
                print(f"【文件检索】原文摘录={'有' if quote else '无'} 结果字数={len(result)}")

            if not result:
                skipped.append({"path": path, "reason": "no_relevant_content"})
                continue
            retrieved_items.append(f"使用file工具以{query}为关键内容，对文件{path}的检索结果：{result}")
            file_retrieved_items[(path,query)]=result

            path_obj=Path(path)
            try:
                file_mtime=datetime.fromtimestamp(path_obj.stat().st_mtime).isoformat()
            except OSError:
                file_mtime=""
            start,end=locate_quote(content, quote_body, query)
            evidence_records.append({
                "evidence_id":"",
                "source_type":"file",
                "source":str(path),
                "sources":[str(path)],
                "source_id":file_digest(path),
                "title":path_obj.stem,
                "suffix":path_obj.suffix.lower(),
                "file_mtime":file_mtime,
                "query":str(query),
                "attempt_id":str(params.get("attempt_id") or ""),
                "snippet":str(result)[:300],
                "content":str(result)[:6000],
                "source_excerpt":str(content)[:10000],
                "paragraph_index":locate_paragraph(content, quote_body, query),
                "char_start":start,
                "char_end":end,
                "extraction_version":EXTRACTION_VERSION,
            })

        return {
            **state,
            "file_retrived_items":file_retrieved_items,
            "retrieved_content":retrieved_items,
            "evidence_records":ensure_evidence_ids(evidence_records),
            "retrieval_tool_calls":int(state.get("retrieval_tool_calls") or 0)+calls_made,
            "retrieval_context_chars":int(state.get("retrieval_context_chars") or 0)+context_used,
            "retrieval_skipped":list(state.get("retrieval_skipped") or [])+skipped,
        }
    return file_retrive
