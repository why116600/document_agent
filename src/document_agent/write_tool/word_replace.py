import re
from typing import Dict, List, Tuple
from uuid import uuid4
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.table import Table

try:
    from document_agent.write_tool.word_tool import element_has_protected_content
except ImportError:
    from word_tool import element_has_protected_content

#哨兵串边界字符：用Unicode私有区，正常文档中不会出现
_SENTINEL_PREFIX = "\ue000"
_SENTINEL_SUFFIX = "\ue001"


def _write_t(t_elem, text: str) -> None:
    #写入 w:t 的文本，并按首尾空格维护 xml:space
    t_elem.text = text
    if text.startswith(" ") or text.endswith(" "):
        t_elem.set(qn("xml:space"), "preserve")
    elif t_elem.get(qn("xml:space")) is not None:
        del t_elem.attrib[qn("xml:space")]


def _set_run_text_keep_children(run: Run, new_text: str) -> None:
    """仅修改或清空 Run 内的 w:t 文本节点，保留其中的 drawing、pict、fldChar、oMath 等非文本子元素。"""
    r_elem = run._r
    t_elems = r_elem.findall(qn("w:t"))
    if not t_elems:
        if new_text:
            t = OxmlElement("w:t")
            t.text = new_text
            if new_text.startswith(" ") or new_text.endswith(" "):
                t.set(qn("xml:space"), "preserve")
            r_elem.append(t)
        return

    #受保护对象（图片/域/公式）两侧的文案分属不同 w:t：整体写进第一个会把对象之后的文案
    #挪到对象之前。若新文案仍以这些尾部文案结尾，就原样保留尾部节点，只改写头部节点。
    tail = "".join(t.text or "" for t in t_elems[1:])
    if tail and new_text.endswith(tail):
        _write_t(t_elems[0], new_text[:len(new_text) - len(tail)])
        return

    _write_t(t_elems[0], new_text)
    for extra_t in t_elems[1:]:
        _write_t(extra_t, "")


def _get_paragraph_runs(p: Paragraph) -> List[Run]:
    """获取段落内的所有 Run（包括顶级 Run 及超链接等容器内的 Run）。"""
    r_elements = p._p.xpath("w:r | w:hyperlink/w:r")
    return [Run(r, p) for r in r_elements]


def replace_in_paragraph(p: Paragraph, old_text: str, new_text: str) -> int:
    """在单个段落中安全替换文本，支持跨 Run（多个文本分块）的文本替换。

    采用单趟逆序替换，彻底避免递归死循环，并保持未替换文本的 Run 格式及非文本内嵌元素（图片、公式、域代码）。
    返回该段落中成功替换的次数。
    """
    if not old_text or old_text not in p.text:
        return 0

    runs = _get_paragraph_runs(p)
    if not runs:
        return 0

    full_text = p.text
    char_map: List[Tuple[int, int]] = []
    for r_idx, run in enumerate(runs):
        for c_idx in range(len(run.text)):
            char_map.append((r_idx, c_idx))

    if len(char_map) != len(full_text):
        return 0

    match_indices = []
    start = 0
    while True:
        pos = full_text.find(old_text, start)
        if pos < 0:
            break
        match_indices.append(pos)
        start = pos + len(old_text)

    if not match_indices:
        return 0

    replaced_count = 0
    # 倒序处理，避免修改后方 Run 影响前面匹配项在 Run 内的偏移
    for start_pos in reversed(match_indices):
        end_pos = start_pos + len(old_text)
        start_run_idx, start_char_idx = char_map[start_pos]
        end_run_idx, end_char_idx = char_map[end_pos - 1]

        # 跨 Run 匹配若跨越含受保护元素（图片/公式/域代码）的中间 Run，跳过以免破坏结构
        if start_run_idx != end_run_idx:
            has_protected_mid = any(
                element_has_protected_content(runs[mid]._r)
                for mid in range(start_run_idx + 1, end_run_idx)
            )
            if has_protected_mid:
                continue

        start_run = runs[start_run_idx]
        end_run = runs[end_run_idx]

        prefix = start_run.text[:start_char_idx]
        suffix = end_run.text[end_char_idx + 1:]

        if start_run_idx == end_run_idx:
            _set_run_text_keep_children(start_run, prefix + new_text + suffix)
        else:
            _set_run_text_keep_children(start_run, prefix + new_text)
            # 清空中间 Run 的文本，保留非文本子节点
            for mid_idx in range(start_run_idx + 1, end_run_idx):
                _set_run_text_keep_children(runs[mid_idx], "")
            _set_run_text_keep_children(end_run, suffix)

        replaced_count += 1

    return replaced_count


def replace_in_table(table: Table, old_text: str, new_text: str) -> int:
    """在表格的所有单元格段落中安全替换文本。"""
    count = 0
    for p in _iter_table_paragraphs(table):
        count += replace_in_paragraph(p, old_text, new_text)
    return count


def _iter_table_paragraphs(table: Table):
    """遍历表格所有单元格段落（合并单元格去重）。"""
    seen = set()
    for row in table.rows:
        for cell in row.cells:
            #合并单元格会在 row.cells 中重复出现且指向同一个 w:tc，去重避免重复处理、计数虚高
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            for p in cell.paragraphs:
                yield p


def _iter_replace_paragraphs(doc: Document):
    """枚举替换范围：正文段落、表格单元格、页眉页脚（含其中表格）。"""
    for p in doc.paragraphs:
        yield p
    for table in doc.tables:
        yield from _iter_table_paragraphs(table)
    for section in doc.sections:
        for container in (section.header, section.footer):
            if container is None:
                continue
            for p in container.paragraphs:
                yield p
            for t in container.tables:
                yield from _iter_table_paragraphs(t)


def replace_in_document(doc: Document, old_text: str, new_text: str) -> int:
    """在整篇 Word 文档（正文所有段落 + 表格所有单元格）中查找替换文本。"""
    if not old_text:
        return 0

    total_count = 0
    for p in _iter_replace_paragraphs(doc):
        total_count += replace_in_paragraph(p, old_text, new_text)

    return total_count


def _make_sentinel(doc_text: str, position: int) -> str:
    """生成唯一的哨兵串；与原文冲突时追加随机后缀直至唯一。"""
    candidate = f"{_SENTINEL_PREFIX}{position}{_SENTINEL_SUFFIX}"
    while candidate in doc_text:
        candidate = f"{_SENTINEL_PREFIX}{position}-{uuid4().hex[:8]}{_SENTINEL_SUFFIX}"
    return candidate


def batch_replace_in_document(doc: Document, replace_pairs: Dict[str, str]) -> Dict[str, int]:
    """批量替换文档中的多组键值对，返回每个目标词在原文中的替换命中次数统计。

    逐对串行替换会有"A→B、B→C"污染（{"甲方":"乙方","乙方":"丙方"}会把甲方最终变成丙方），
    故改用占位符两阶段替换，命中数也只统计原文中的真实出现次数。
    """
    stats: Dict[str, int] = {}
    #长词优先：{"甲":"X","甲方":"Y"} 这类有重叠的键，短词先替换会吃掉长词的文本
    valid_pairs = sorted(
        ((old, str(new)) for old, new in (replace_pairs or {}).items() if old),
        key=lambda pair: -len(pair[0]),
    )
    if not valid_pairs:
        return stats

    doc_text = "\n".join(p.text or "" for p in _iter_replace_paragraphs(doc))

    #阶段一：旧词 -> 哨兵串，此时的命中数即原文真实出现次数
    sentinel_of: Dict[str, str] = {}
    for position, (old_text, _) in enumerate(valid_pairs):
        sentinel = _make_sentinel(doc_text, position)
        sentinel_of[old_text] = sentinel
        stats[old_text] = replace_in_document(doc, old_text, sentinel)

    #阶段二：哨兵串 -> 新词，新词里即使含其它组的旧词也不会被再替换
    for old_text, new_text in valid_pairs:
        replace_in_document(doc, sentinel_of[old_text], new_text)

    return stats
