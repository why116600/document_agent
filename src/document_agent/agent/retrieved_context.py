"""写作节点的「检索资料」渲染：把 ``retrieved_content`` 变成提示词里的资料块。

检索阶段有两种模式，产出的行格式不同：

* **本地图数据库**（``gdb_retrieve_node``）：以“可展开/不可展开的图数据库节点<id>的内容：…”
  为主的成段摘要；
* **远程知识图谱**（``kg_retrieve_node``）：带 ``【实体】【关系】【分块】【原文】【路径】``
  等来源标记的结构化行。

写作节点需要的都只是一段可读文本，但远程结果更适合“按来源使用”：``【原文】`` 作直接依据、
实体关系作背景、不要臆造图谱里没有的东西。所以这里统一渲染：

* 本地模式：输出与改造前**逐字相同**（``header + "\\n" + 行拼接``），老流程零影响；
* 远程模式：在资料前加一段来源说明、在资料后加使用约束，并按总字数截断以保护上下文。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

LOCAL_MODE = "local"
REMOTE_MODE = "remote"

# 远程知识图谱结果的来源标记说明（与 kg_retrieve_node 的渲染保持一致）
REMOTE_SOURCE_NOTICE = (
    "以下资料来自系统「远程知识图谱」，行首标记表示来源："
    "【可引用文档】本次可引用的文档完整名称；"
    "【文档】文档元信息；【实体】图谱实体及其描述；【关系】实体间关系；"
    "【分块】原文片段摘要；【原文】分块原文；【路径】实体间关系链；"
    "【实体提及】用户说法在图谱中对齐到的实体。"
)
REMOTE_CITATION_RULE = (
    "【引用规范】正文中引用知识图谱里的文档时，必须逐字使用【可引用文档】列出的完整文档名称"
    "（含扩展名），并用《》括起；禁止使用简称、缩写、省略扩展名，也禁止把分块标题、"
    "章节或附件标题（如《附件1》）当作文档名。若不确定完整名称，宁可不引用也不要简写。"
)
REMOTE_USAGE_HINT = (
    "写作时以【原文】作为可直接引用的依据，【实体】【关系】【路径】用于补充背景与因果；"
    "不要把图谱里没有的实体、数字或结论写成事实。"
)

# 远程模式下塞进写作提示词的资料上限（字符），避免多轮检索把上下文撑爆
DEFAULT_REMOTE_LIMIT = 24000


def _normalize_lines(items: Optional[Iterable[Any]]) -> List[str]:
    """过滤空项、统一成字符串，保持原有顺序。"""
    lines: List[str] = []
    for item in items or []:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            lines.append(text)
    return lines


def build_retrieved_info(
    items: Optional[Iterable[Any]],
    *,
    mode: str = LOCAL_MODE,
    header: str = "目前已知：",
    trailing_newline: bool = False,
    remote_limit: int = DEFAULT_REMOTE_LIMIT,
) -> str:
    """把检索结果渲染成写作提示词中的资料块；没有内容时返回空字符串。

    :param items: ``state["retrieved_content"]``
    :param mode: ``local``（保持原有格式）或 ``remote``（加来源说明与使用约束）
    :param header: 块首说明，沿用各节点原来的措辞
    :param trailing_newline: 是否在末尾补一个换行（改写节点原来是这么拼的）
    :param remote_limit: 远程模式下的资料字数上限，``0`` 表示不限制
    """
    lines = _normalize_lines(items)
    if not lines:
        return ""

    if str(mode).strip().lower() != REMOTE_MODE:
        block = f"{header}\n" + "\n".join(lines)
        return block + ("\n" if trailing_newline else "")

    body = "\n".join(lines)
    if remote_limit > 0 and len(body) > remote_limit:
        body = body[:remote_limit].rstrip() + "\n……（检索资料较长，已截断）"
    block = (
        f"{header}\n{REMOTE_SOURCE_NOTICE}\n{REMOTE_CITATION_RULE}\n{body}\n{REMOTE_USAGE_HINT}"
    )
    return block + ("\n" if trailing_newline else "")


def _doc_stem(full_name: str) -> str:
    """取文档名去掉扩展名后的部分（引用里常见“标题不带扩展名”）。"""
    return full_name.rsplit(".", 1)[0] if "." in full_name else full_name


def enforce_full_document_names(
    doc: Any,
    citations: Optional[Iterable[Any]],
) -> List[Tuple[str, str, int]]:
    """把正文里用简称写的《…》引用补全成知识图谱里的完整文档名。

    只在远程知识图谱模式下调用：检索结果同时给出完整文件名（``path`` 的文件名）与图谱
    标题（可能只是简称），模型偶尔会把简称、甚至“附件1”这类章节标题当文档名来引用，
    这里按 ``《简称》 -> 《完整文件名》`` 做一次确定性补全，返回实际发生替换的条目。

    :param doc: ``python-docx`` 的 ``Document`` 对象（正文与表格都会被处理）
    :param citations: ``state["document_citations"]``，每项含 ``full_name`` / ``title``
    :return: ``[(原文, 替换为, 命中次数), ...]``；没有可补全的引用时为空列表
    """
    if not citations:
        return []

    # 简称 → 完整名。同一个简称对应多篇文档时保持原样（无法判断该补成哪一篇）
    candidates: Dict[str, Set[str]] = {}
    full_names: Set[str] = set()
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        full_name = str(citation.get("full_name") or "").strip()
        if not full_name:
            continue
        full_names.add(full_name)
        aliases = {str(citation.get("title") or "").strip(), _doc_stem(full_name)}
        for alias in aliases:
            if not alias or alias == full_name:
                continue
            candidates.setdefault(alias, set()).add(full_name)

    pairs: Dict[str, str] = {}
    for alias, names in candidates.items():
        if len(names) != 1 or alias in full_names:
            continue
        pairs[f"《{alias}》"] = f"《{next(iter(names))}》"
    if not pairs:
        return []

    from document_agent.write_tool.word_replace import batch_replace_in_document

    stats = batch_replace_in_document(doc, pairs)
    return [(old, pairs[old], count) for old, count in stats.items() if count]


__all__ = [
    "LOCAL_MODE",
    "REMOTE_MODE",
    "REMOTE_SOURCE_NOTICE",
    "REMOTE_CITATION_RULE",
    "REMOTE_USAGE_HINT",
    "DEFAULT_REMOTE_LIMIT",
    "build_retrieved_info",
    "enforce_full_document_names",
]
