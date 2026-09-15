import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import BaseModel, Field

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.write_tool.word_tool import (
    ParagraphItem,
    TableItem,
    TextRunItem,
    convert_node,
    replace_node_with_data,
    resolve_save_path,
    save_document,
)

DEFAULT_FONT_SIZE = 12
DEFAULT_FONT_COLOR = "#000000"

MAX_DESCRIPTION_CHARS = 8000#文档结构描述超过该字符数时启用工具化按需改写，避免超出上下文窗口
MAX_TOOL_ROUNDS = 20#工具化改写的最大轮数
SEARCH_MAX_HITS = 20#search工具最多返回的命中数
MAX_READ_CHARS = 4000#read工具单次最多返回的字符数，避免一次读入过多内容
SEARCH_CONTEXT_BEFORE = 50#search结果中关键词前保留的字符数
SEARCH_CONTEXT_AFTER = 60#search结果中关键词后保留的字符数
HEADING_MAX_CHARS = 35#启发式判定标题时的最大字数
HEADING_MIN_FONT_SIZE = 14#启发式判定标题时的大字号阈值
VIRTUAL_BLOCK_SIZE = 18#无标题文档按多少个元素聚合成一个虚拟块
BLOCK_PREVIEW_CHARS = 40#虚拟块首尾句保留的字符数

#启发式标题的编号规则：正则命中即视为标题，第一个元素是该标题的层级
HEADING_PATTERNS = [
    (1, re.compile(r"^第[一二三四五六七八九十百千0-9]+[章节]")),
    (1, re.compile(r"^【.+?】")),
    (2, re.compile(r"^第[一二三四五六七八九十百千0-9]+[条]")),
    (2, re.compile(r"^[一二三四五六七八九十]+[、.．]")),
    (3, re.compile(r"^（[一二三四五六七八九十0-9]+）")),
    (3, re.compile(r"^\d+(\.\d+)*[、.．\s]")),
]


class ParagraphEdit(BaseModel):#对文档中单个元素的修改
    index: int = Field(description="要修改的元素下标，与待改写文档结构中的index一致")
    action: str = Field(description="修改动作，replace表示替换该元素内容，delete表示删除该元素，insert_after表示在该元素之后插入一个新段落")
    paragraph: Optional[ParagraphItem] = Field(default=None, description="action为replace或insert_after时，该段落改写后或新增的内容")
    table: Optional[TableItem] = Field(default=None, description="当被修改的元素是表格且需要替换整张表格内容时使用该字段")
    reason: str = Field(default="", description="本次修改的原因")


class RewritePlan(BaseModel):#一次改写的完整方案
    summary: str = Field(default="", description="本次改写的整体思路")
    edits: List[ParagraphEdit] = Field(default_factory=list, description="对文档的具体修改列表，没有需要修改的元素时为空列表")

class RewriteToolCall(BaseModel):#改写过程中模型选择要调用的工具
    tool_name: str = Field(description="要调用的工具名称，可选值：outline查看文档目录，search按关键词搜索，read读取某段元素，edit提交对某个元素的修改，end结束改写")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="调用工具所需的参数")


def _run_to_dict(run: TextRunItem) -> dict:
    result = {"text": run.text}
    if run.bold:
        result["bold"] = True
    if run.italic:
        result["italic"] = True
    if run.underline:
        result["underline"] = True
    if run.font_size and run.font_size != DEFAULT_FONT_SIZE:
        result["font_size"] = run.font_size
    if run.font_color and run.font_color.upper() != DEFAULT_FONT_COLOR:
        result["font_color"] = run.font_color
    return result


def _paragraph_to_dict(item: ParagraphItem) -> dict:
    result = {"runs": [_run_to_dict(run) for run in item.runs]}
    if item.alignment and item.alignment != "left":
        result["alignment"] = item.alignment
    return result


def _element_text_length(item: dict) -> int:#估算单个元素的文本长度
    if item.get("type") == "paragraph":
        return sum(len(run.get("text", "")) for run in item.get("runs", []))
    if item.get("type") == "table":
        return sum(len(str(cell)) for row in item.get("cells", []) for cell in row)
    return 0


def _description_size(description) -> int:#估算结构描述的总字符数
    return sum(_element_text_length(item) for item in description)


def _item_text(item: dict) -> str:
    #取出元素的纯文本，用于关键词搜索
    if item.get("type") == "paragraph":
        return "".join(run.get("text", "") for run in item.get("runs", []))
    if item.get("type") == "table":
        return " ".join(str(cell) for row in item.get("cells", []) for cell in row)
    return ""


def _get_outline_level(node):
    #读取段落的大纲级别（w:outlineLvl），没有设置时返回None
    try:
        p_pr = node._element.find(qn("w:pPr"))
        if p_pr is None:
            return None
        outline = p_pr.find(qn("w:outlineLvl"))
        if outline is None:
            return None
        value = outline.get(qn("w:val"))
        return int(value) if value is not None else None
    except Exception:
        return None


def _looks_like_visual_heading(base_item) -> bool:
    #正文里"加粗或大字号"的短段落，作为无编号标题的辅助线索
    if not isinstance(base_item, ParagraphItem) or not base_item.runs:
        return False
    first_run = base_item.runs[0]
    if first_run.bold:
        return True
    return bool(first_run.font_size and first_run.font_size >= HEADING_MIN_FONT_SIZE)


def _detect_heading(node, base_item, text):
    """识别标题，返回(层级, 标题文本)，不是标题时返回None。

    依次尝试：标准标题样式 → 大纲级别 → 编号正则 → 短文本且加粗/大字号。
    """
    if not text:
        return None
    style_name = ""
    try:
        style_name = node.style.name or ""
    except Exception:
        style_name = ""
    if style_name.startswith("Heading") or style_name.startswith("标题"):
        digits = re.findall(r"\d+", style_name)
        level = int(digits[0]) if digits else 1
        return max(1, min(level, 4)), text
    outline_level = _get_outline_level(node)
    if outline_level is not None:
        return max(1, min(outline_level + 1, 4)), text
    if len(text) > HEADING_MAX_CHARS:#启发式只对短文本生效，避免误判正文
        return None
    for level, pattern in HEADING_PATTERNS:
        if pattern.match(text):
            return level, text
    if _looks_like_visual_heading(base_item):
        return 2, text
    return None


def _context_window(text: str, position: int, keyword_length: int) -> str:
    #以关键词命中位置为中心截取前后文，供search工具返回自解释的片段
    start = max(0, position - SEARCH_CONTEXT_BEFORE)
    end = min(len(text), position + keyword_length + SEARCH_CONTEXT_AFTER)
    window = text[start:end]
    if start > 0:
        window = "…" + window
    if end < len(text):
        window = window + "…"
    return window


def _range_stats(description, start: int, end: int) -> dict:
    #统计某个下标区间内的元素构成与字数
    items = description[start:end + 1]
    paragraphs = sum(1 for item in items if item.get("type") == "paragraph")
    tables = sum(1 for item in items if item.get("type") == "table")
    return {"elements": len(items), "paragraphs": paragraphs, "tables": tables, "chars": _description_size(items)}


def _ranges_valid(ranges, total: int) -> bool:
    #校验大纲区间是否连续覆盖0~total-1且不重叠，防止识别错误导致后续读写错位
    if not ranges or total <= 0:
        return False
    ordered = sorted(ranges)
    if ordered[0][0] != 0 or ordered[-1][1] != total - 1:
        return False
    for (start, end), (next_start, _) in zip(ordered, ordered[1:]):
        if start > end or next_start != end + 1:
            return False
    return True


def _to_tree_entries(sections, description) -> list:
    #把扁平的章节列表按层级嵌套成树
    entries = []
    stack = []
    for section in sections:
        entry = {
            "level": section["level"],
            "title": section["title"],
            "index_range": [section["start"], section["end"]],
            "stats": _range_stats(description, section["start"], section["end"]),
            "children": [],
        }
        while stack and stack[-1]["level"] >= entry["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(entry)
        else:
            entries.append(entry)
        stack.append(entry)
    return entries


def _build_virtual_blocks(description, block_size: int = VIRTUAL_BLOCK_SIZE) -> list:
    #无标题文档的兜底：按固定元素数聚合成虚拟块，用首尾句作为定位线索
    blocks = []
    total = len(description)
    for start in range(0, total, block_size):
        end = min(total - 1, start + block_size - 1)
        items = description[start:end + 1]
        blocks.append({
            "index_range": [start, end],
            "stats": _range_stats(description, start, end),
            "head": _item_text(items[0])[:BLOCK_PREVIEW_CHARS],
            "tail": _item_text(items[-1])[:BLOCK_PREVIEW_CHARS],
        })
    return blocks


def build_outline_tree(nodes, description, base_items) -> dict:
    """构建文档大纲，返回 {summary, outline}。

    优先按标题层级成树（Heading样式 → 大纲级别 → 编号正则 → 短文本加粗/大字号）；
    识别不出有效标题、或区间校验不通过时，退化为按固定元素数聚合的虚拟块。
    """
    total = len(description)
    summary = _range_stats(description, 0, total - 1) if total else {
        "elements": 0, "paragraphs": 0, "tables": 0, "chars": 0}
    sections = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Paragraph):
            continue
        detected = _detect_heading(node, base_items[index], (node.text or "").strip())
        if detected is None:
            continue
        level, title = detected
        sections.append({"level": level, "title": title[:60], "start": index, "end": index})
    if len(sections) >= 2:#只识别出一个章节时，树没有导航价值，直接走虚拟块
        if sections[0]["start"] > 0:#文档开头到第一个标题之间的内容，与首个标题同级
            first_level = sections[0]["level"]
            sections.insert(0, {"level": first_level, "title": "（文档开头）", "start": 0, "end": 0})
        for position, section in enumerate(sections):
            if position + 1 < len(sections):
                section["end"] = sections[position + 1]["start"] - 1
            else:
                section["end"] = total - 1
        if _ranges_valid([(section["start"], section["end"]) for section in sections], total):
            summary["mode"] = "heading_tree"
            summary["section_count"] = len(sections)
            return {"summary": summary, "outline": _to_tree_entries(sections, description)}
        print("大纲区间校验未通过，降级为虚拟块")
    summary["mode"] = "virtual_blocks"
    summary["block_size"] = VIRTUAL_BLOCK_SIZE
    return {"summary": summary, "outline": _build_virtual_blocks(description)}


TOOL_DESCRIPTION = (
    "可用工具：\n"
    "- outline：查看文档大纲，返回全文概览与章节树（或虚拟块），每项含 level、title、index_range[起,止]、stats（元素/段落/表格/字数），不返回正文；\n"
    "- search：参数query，按关键词搜索，返回命中元素的index与该关键词前后的上下文（表格返回表头与命中的单元格）；\n"
    "- read：参数start_index、end_index，读取该范围内元素的完整内容，下标是全文全局下标；\n"
    "- edit：参数index、action、paragraph或table、reason，提交对某个元素的修改，action可选replace/delete/insert_after；\n"
    "- end：结束改写，应用所有已经提交的修改。\n"
    "推荐流程：先用 outline 定位目标章节的 index_range；需要确认细节时再用 search 或 read；"
    "如果 search 返回的上下文已经足够，可以直接 edit；最后用 end 结束。\n"
)


def _tool_outline(outline_tree, arguments) -> str:
    #返回文档大纲（标题树或虚拟块），不包含正文内容
    return json.dumps(outline_tree, ensure_ascii=False)


def _search_in_table(item, lowered_query, keyword_length):
    #表格命中时只返回表头与命中的那个单元格，避免把整张表拼成一段乱序文本
    cells = item.get("cells", [])
    header = cells[0] if cells else []
    for row_index, row in enumerate(cells):
        for col_index, cell in enumerate(row):
            cell_text = str(cell)
            position = cell_text.lower().find(lowered_query)
            if position >= 0:
                return {
                    "index": item["index"],
                    "type": "table",
                    "header": header,
                    "hit_cell": {
                        "row": row_index,
                        "col": col_index,
                        "context": _context_window(cell_text, position, keyword_length),
                    },
                }
    return None


def _tool_search(description, arguments) -> str:
    #按关键词搜索文档内容，返回命中元素的全局下标与关键词前后文
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "search工具反馈：query不能为空"
    lowered = query.lower()
    hits = []
    for item in description:
        if item.get("type") == "table":
            hit = _search_in_table(item, lowered, len(query))
            if hit:
                hits.append(hit)
        else:
            text = _item_text(item)
            position = text.lower().find(lowered)
            if position >= 0:
                hits.append({
                    "index": item["index"],
                    "type": "paragraph",
                    "context": _context_window(text, position, len(query)),
                })
        if len(hits) >= SEARCH_MAX_HITS:
            break
    if not hits:
        return f"search工具反馈：没有找到包含“{query}”的内容"
    return json.dumps(hits, ensure_ascii=False)


def _tool_read(description, arguments) -> str:
    #读取指定范围内元素的完整内容，单次读取量有上限
    try:
        start = int(arguments.get("start_index", 0))
        end = int(arguments.get("end_index", start))
    except (TypeError, ValueError):
        return "read工具反馈：start_index与end_index必须是整数"
    if start > end:
        return "read工具反馈：start_index不能大于end_index"
    start = max(0, start)
    end = min(len(description) - 1, end)
    if start > end:
        return f"read工具反馈：下标越界，文档元素的下标范围是0~{len(description) - 1}"
    chunk = description[start:end + 1]
    text = json.dumps(chunk, ensure_ascii=False)
    if len(text) > MAX_READ_CHARS:
        return (
            f"read工具反馈：请求的范围过大（约{len(text)}字，单次上限{MAX_READ_CHARS}字），"
            f"请缩小start_index与end_index后重新读取。该范围开头的元素：{json.dumps(chunk[:3], ensure_ascii=False)}"
        )
    return text


def _tool_edit(description, arguments, edited) -> str:
    #校验并记录一条修改，校验不通过时把原因反馈给模型
    try:
        edit = ParagraphEdit.model_validate(arguments)
    except Exception as e:
        return f"edit工具反馈：修改内容不合法（{e}）"
    if edit.index < 0 or edit.index >= len(description):
        return f"edit工具反馈：下标{edit.index}越界，文档元素的下标范围是0~{len(description) - 1}"
    action = (edit.action or "").strip().lower()
    if action not in ("replace", "delete", "insert_after"):
        return f"edit工具反馈：不支持的动作{edit.action}，只支持replace/delete/insert_after"
    if action == "replace" and edit.paragraph is None and edit.table is None:
        return "edit工具反馈：replace必须提供paragraph（段落）或table（表格）字段"
    if action == "insert_after" and edit.paragraph is None:
        return "edit工具反馈：insert_after必须提供paragraph字段"
    if (edit.index, action) in {(item.index, (item.action or "").strip().lower()) for item in edited}:
        return f"edit工具反馈：下标{edit.index}的{action}已经提交过，忽略重复修改"
    edited.append(edit)
    return f"edit工具反馈：已记录对下标{edit.index}的{action}修改，当前共{len(edited)}条"


def _execute_tool(tool, description, outline_tree, edited) -> str:
    #执行模型选择的工具，返回给模型的反馈文本
    name = (tool.tool_name or "").strip().lower()
    arguments = tool.arguments or {}
    if name == "outline":
        return _tool_outline(outline_tree, arguments)
    if name == "search":
        return _tool_search(description, arguments)
    if name == "read":
        return _tool_read(description, arguments)
    if name == "edit":
        return _tool_edit(description, arguments, edited)
    if name == "end":
        return f"end工具反馈：结束改写，共提交{len(edited)}条修改"
    return f"未知工具{name}，可用工具：outline/search/read/edit/end"


def _rewrite_with_tools(llm, dialog, nodes, description, base_items):
    #工具化改写：先构建大纲，再由模型按需探查文档并逐条提交修改，返回(修改列表, 错误信息)
    outline_tree = build_outline_tree(nodes, description, base_items)
    summary = outline_tree.get("summary", {})
    print(f"文档大纲模式：{summary.get('mode')}，元素{summary.get('elements')}个"
          f"（段落{summary.get('paragraphs')}、表格{summary.get('tables')}、约{summary.get('chars')}字）")
    for entry in outline_tree.get("outline", [])[:10]:
        print("   ", entry.get("title") or entry.get("head") or "", entry.get("index_range"))
    edited = []
    feedback = ""
    for _ in range(MAX_TOOL_ROUNDS):
        think_prompt = (
            "你是文档改写助手。这份文档很长，不要把全文读进来，而是用工具按需探查和修改。\n"
            f"{TOOL_DESCRIPTION}"
            f"用户对话内容：{dialog}\n"
            f"{feedback}当前已提交的修改数量：{len(edited)}\n"
            "请分析下一步应该调用哪个工具，以及为什么。"
        )
        analysis = getattr(llm_invoke(llm, think_prompt), "content", "")
        action_prompt = (
            "你是文档改写助手，请根据分析选择并调用工具。\n"
            f"{TOOL_DESCRIPTION}"
            f"用户对话内容：{dialog}\n"
            f"{feedback}当前已提交的修改数量：{len(edited)}\n"
            f"本轮分析思路：{analysis}\n"
            "请严格按照上述分析调用工具。"
        )
        tool = llm_model_invoke(llm, action_prompt, RewriteToolCall)
        if tool is None:
            feedback += "\n工具调用失败，请重新选择工具"
            continue
        result = _execute_tool(tool, description, outline_tree, edited)
        print(f"改写工具：{tool.tool_name} {tool.arguments} -> {result[:200]}")
        feedback += f"\n调用{tool.tool_name}({tool.arguments}) -> {result}"
        if (tool.tool_name or "").strip().lower() == "end":
            return edited, ""
    return edited, f"改写超过{MAX_TOOL_ROUNDS}轮仍未结束，请把改写要求拆成更小的任务"


def build_document_description(doc: Document):
    #解析文档的每个元素，使得改写的时候能够准确定位
    nodes = []
    base_items = []
    description = []
    for index, node in enumerate(doc.iter_inner_content()):
        nodes.append(node)
        if isinstance(node, Paragraph):
            try:
                item = convert_node(node)
            except Exception as e:
                print(f"解析第{index}个段落失败，按纯文本处理：{e}")
                item = ParagraphItem(runs=[TextRunItem(text=node.text, text_type="text")])
            base_items.append(item)
            description.append({"index": index, "type": "paragraph", **_paragraph_to_dict(item)})
        elif isinstance(node, Table):
            base_items.append(None)
            cells = [[cell.text for cell in row.cells] for row in node.rows]
            description.append({
                "index": index,
                "type": "table",
                "rows": len(cells),
                "cols": len(cells[0]) if cells else 0,
                "cells": cells,
            })
        else:
            base_items.append(None)
            description.append({"index": index, "type": "unknown"})
    return nodes, base_items, description #返回对象、对应格式模型、结构描述

def _inherit_run_format(new_run: TextRunItem, base_run: Optional[TextRunItem]) -> TextRunItem:
    #模型显式给出的格式优先（包括显式取消格式），只有没给出（None）的字段才沿用原文格式。
    if base_run is None:
        return new_run
    return TextRunItem(
        text=new_run.text,
        text_type=new_run.text_type,
        bold=base_run.bold if new_run.bold is None else new_run.bold,
        italic=base_run.italic if new_run.italic is None else new_run.italic,
        underline=base_run.underline if new_run.underline is None else new_run.underline,
        font_size=base_run.font_size if new_run.font_size is None else new_run.font_size,
        font_color=base_run.font_color if new_run.font_color is None else new_run.font_color,
    )


def _inherit_paragraph_format(new_item: ParagraphItem, base_item) -> ParagraphItem:
    #让改写后的段落尽量沿用原段落的格式：模型给了什么就用什么，没给（None）的才沿用原文。
    if not isinstance(base_item, ParagraphItem) or not base_item.runs:
        return new_item
    base_run = base_item.runs[0]
    return ParagraphItem(
        runs=[_inherit_run_format(run, base_run) for run in new_item.runs],
        alignment=base_item.alignment if new_item.alignment is None else new_item.alignment,
        spacing_before=base_item.spacing_before if new_item.spacing_before is None else new_item.spacing_before,
        spacing_after=base_item.spacing_after if new_item.spacing_after is None else new_item.spacing_after,
        line_spacing=base_item.line_spacing if new_item.line_spacing is None else new_item.line_spacing,
    )


def apply_rewrite_plan(doc: Document, nodes, base_items, edits) -> int:
    #修改按下标从后往前执行，避免插入/删除导致后续下标错位；同一下标只处理第一条修改。
    applied = 0
    handled = set()
    # 同一下标允许同时"替换"和"追加"，因此按动作优先级排序；下标从大到小处理，避免增删导致下标错位
    action_priority = {"replace": 0, "insert_after": 1, "delete": 2}
    ordered_edits = sorted(
        edits or [],
        key=lambda item: (-item.index, action_priority.get((item.action or "").strip().lower(), 9)),
    )
    for edit in ordered_edits:
        if edit.index < 0 or edit.index >= len(nodes):
            print(f"忽略越界的修改下标：{edit.index}")
            continue
        node = nodes[edit.index]
        base_item = base_items[edit.index]
        action = (edit.action or "").strip().lower()
        edit_key = (edit.index, action)
        if edit_key in handled:
            print(f"忽略重复的修改：下标{edit.index}动作{edit.action}")
            continue
        handled.add(edit_key)
        try:
            if action == "delete":
                node._element.getparent().remove(node._element)
                applied += 1
                print(f"已删除第{edit.index}个元素")
            elif action == "replace":
                if isinstance(node, Table):
                    if not isinstance(edit.table, TableItem):
                        print(f"第{edit.index}个元素是表格，但模型没有给出表格内容，跳过")
                        continue
                    replace_node_with_data(node, edit.table, doc)
                else:
                    if not isinstance(edit.paragraph, ParagraphItem):
                        print(f"第{edit.index}个元素是段落，但模型没有给出段落内容，跳过")
                        continue
                    replace_node_with_data(node, _inherit_paragraph_format(edit.paragraph, base_item), doc)
                applied += 1
                print(f"已改写第{edit.index}个元素")
            elif action == "insert_after":
                if not isinstance(edit.paragraph, ParagraphItem):
                    print(f"第{edit.index}个元素的插入内容为空，跳过")
                    continue
                new_paragraph = doc.add_paragraph("")
                replace_node_with_data(new_paragraph, _inherit_paragraph_format(edit.paragraph, base_item))
                # add_paragraph 会把段落追加到文档末尾，这里移动到目标元素之后
                node._element.addnext(new_paragraph._element)
                applied += 1
                print(f"已在第{edit.index}个元素后插入新段落")
            else:
                print(f"忽略未知的修改动作：{edit.action}")
        except Exception as e:
            print(f"应用第{edit.index}个元素的修改失败：{e}")
    return applied

def create_rewrite_node(llm):#改写已有文档的节点
    def rewrite_node(state: AgentState) -> AgentState:
        rewrite_path = state.get("rewrite_file")
        if not rewrite_path:
            return {**state, "state": "error", "error": "没有指定需要改写的文档路径"}
        path_obj = Path(rewrite_path)
        if not path_obj.exists():
            return {**state, "state": "error", "error": f"需要改写的文档不存在：{rewrite_path}"}
        if path_obj.suffix.lower() != ".docx":
            return {**state, "state": "error", "error": f"目前只支持改写docx文档：{rewrite_path}"}

        doc = Document(str(path_obj))
        nodes, base_items, description = build_document_description(doc)
        if not nodes:
            return {**state, "state": "error", "error": f"文档内容为空，无法改写：{rewrite_path}"}
        print(f"待改写文档共{len(nodes)}个元素")

        retrieved_items = state.get("retrieved_content") or []
        retrieved_info = ""
        if retrieved_items:
            retrieved_info = "可供参考的信息：\n" + "\n".join(retrieved_items) + "\n"

        description_size = _description_size(description)
        dialog = f"{state['messages']}"
        plan = None
        if description_size <= MAX_DESCRIPTION_CHARS:
            #短文档：整篇结构一次性交给模型，一次调用完成
            print(f"文档结构描述约{description_size}字，直接整篇改写")
            prompt = (
                "你是一个企业文档改写助手，需要按照用户的要求改写已有文档。\n"
                "你只能通过给出修改列表来修改文档，不要重写整篇文档。\n"
                f"用户对话内容：{dialog}\n"
                f"{retrieved_info}"
                "待改写文档的结构如下，items中每个元素的下标index与文档中的位置一一对应，请严格使用这些下标：\n"
                f"{json.dumps(description, ensure_ascii=False)}\n"
                "改写要求：\n"
                "1. 只输出需要修改的元素，不需要修改的元素不要出现在edits中；\n"
                "2. action为replace时，用paragraph字段给出该段落改写后的完整内容；如果该元素是表格，用table字段给出新的表格内容；\n"
                "3. action为insert_after时，用paragraph字段给出要插入的新段落；\n"
                "4. action为delete时表示删除该元素；\n"
                "5. 没有特别指明格式时，请沿用原文格式（例如标题加粗、居中），不要无故改变格式；\n"
                "6. 不要改动用户没有要求修改的内容。\n"
            )
            plan = llm_model_invoke(llm, prompt, RewritePlan)
            if plan is None:
                return {**state, "state": "error", "error": "生成文档改写方案失败"}
            print("改写思路：", plan.summary)
        else:
            #长文档：用工具按需探查和修改，上下文里只出现模型真正读到的片段
            print(f"文档结构描述约{description_size}字，超过{MAX_DESCRIPTION_CHARS}字，启用工具化按需改写")
            edits, error = _rewrite_with_tools(llm, f"{dialog}\n{retrieved_info}", nodes, description, base_items)
            if error:
                return {**state, "state": "error", "error": error}
            if not edits:
                return {**state, "state": "error", "error": "模型没有提交任何修改"}
            plan = RewritePlan(summary=f"通过工具按需探查后提交了{len(edits)}条修改", edits=edits)

        applied = apply_rewrite_plan(doc, nodes, base_items, plan.edits)
        if applied == 0:
            print("模型没有给出有效的修改，文档内容保持不变")

        if state.get("save_path"):
            save_path = resolve_save_path(state["save_path"])#有指定路径就另存，复用补后缀和建目录
        else:
            save_path = Path(rewrite_path)#没有指定路径时默认原地覆盖原文件
        save_document(doc, save_path)
        print("改写后的文档已保存到：", str(save_path))
        return {**state, "state": "succeeded", "save_path": str(save_path)}
    return rewrite_node
