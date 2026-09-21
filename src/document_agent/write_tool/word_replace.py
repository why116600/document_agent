import re
from typing import Dict, List, Tuple
from docx import Document
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.table import Table


def _get_paragraph_runs(p: Paragraph) -> List[Run]:
    """获取段落内的所有 Run（包括顶级 Run 及超链接等容器内的 Run）。"""
    r_elements = p._p.xpath("w:r | w:hyperlink/w:r")
    return [Run(r, p) for r in r_elements]


def replace_in_paragraph(p: Paragraph, old_text: str, new_text: str) -> int:
    """在单个段落中安全替换文本，支持跨 Run（多个文本分块）的文本替换。

    采用单趟逆序替换，彻底避免递归死循环，并保持未替换文本的 Run 格式。
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

    # 倒序处理各个匹配项，避免对后方 Run 的修改影响前面匹配项在 run 内部的起始偏移
    for start_pos in reversed(match_indices):
        end_pos = start_pos + len(old_text)
        start_run_idx, start_char_idx = char_map[start_pos]
        end_run_idx, end_char_idx = char_map[end_pos - 1]

        start_run = runs[start_run_idx]
        end_run = runs[end_run_idx]

        prefix = start_run.text[:start_char_idx]
        suffix = end_run.text[end_char_idx + 1:]

        if start_run_idx == end_run_idx:
            start_run.text = prefix + new_text + suffix
        else:
            start_run.text = prefix + new_text
            # 清空中间的 Run 文本
            for mid_idx in range(start_run_idx + 1, end_run_idx):
                runs[mid_idx].text = ""
            end_run.text = suffix

    return len(match_indices)


def replace_in_table(table: Table, old_text: str, new_text: str) -> int:
    """在表格的所有单元格段落中安全替换文本。"""
    count = 0
    for row in table.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                count += replace_in_paragraph(p, old_text, new_text)
    return count


def replace_in_document(doc: Document, old_text: str, new_text: str) -> int:
    """在整篇 Word 文档（正文所有段落 + 表格所有单元格）中查找替换文本。"""
    if not old_text:
        return 0

    total_count = 0
    # 1. 替换正文段落
    for p in doc.paragraphs:
        total_count += replace_in_paragraph(p, old_text, new_text)

    # 2. 替换表格单元格中的段落
    for table in doc.tables:
        total_count += replace_in_table(table, old_text, new_text)

    # 3. 替换页眉页脚（如有）
    for section in doc.sections:
        if section.header:
            for p in section.header.paragraphs:
                total_count += replace_in_paragraph(p, old_text, new_text)
            for t in section.header.tables:
                total_count += replace_in_table(t, old_text, new_text)
        if section.footer:
            for p in section.footer.paragraphs:
                total_count += replace_in_paragraph(p, old_text, new_text)
            for t in section.footer.tables:
                total_count += replace_in_table(t, old_text, new_text)

    return total_count


def batch_replace_in_document(doc: Document, replace_pairs: Dict[str, str]) -> Dict[str, int]:
    """批量替换文档中的多组键值对，返回每个目标词的替换命中次数统计。"""
    stats: Dict[str, int] = {}
    for old_text, new_text in replace_pairs.items():
        if not old_text:
            continue
        c = replace_in_document(doc, old_text, str(new_text))
        stats[old_text] = c
    return stats
