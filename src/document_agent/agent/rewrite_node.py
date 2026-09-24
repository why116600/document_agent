import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import BaseModel, Field, model_validator

try:
    from document_agent.agent.llm_client import llm_invoke, llm_model_invoke
    from document_agent.agent.agent_core import AgentState
except ImportError:
    from llm_client import llm_invoke, llm_model_invoke
    from agent_core import AgentState
from document_agent.write_tool.word_tool import (
    DEFAULT_FONT_SIZE,
    DEFAULT_FONT_COLOR,
    ParagraphItem,
    TableItem,
    TextRunItem,
    DocxRoot,
    convert_node,
    element_has_protected_content,
    paragraph_has_protected_content,
    replace_node_with_data,
    fill_table_from_grid,
    resolve_save_path,
    save_document,
)
from document_agent.write_tool.word_replace import (
    batch_replace_in_document,
    replace_in_paragraph,
    replace_in_table,
)

TOOL_DESCRIPTION = """你有以下工具可以使用：
1. outline: 查看文档的大纲目录与章节结构。参数为空字典 {}。
2. search: 按关键词搜索文档内容，返回匹配项所在元素的下标 index 与前后文。参数: {"query": "关键词"}。
3. read: 读取指定下标范围内的元素完整内容。参数: {"start_index": 起始下标, "end_index": 结束下标}。
4. edit: 提交对某个元素的修改（暂存到修改队列）。支持单条修改或批量修改：
   - 单条参数: {"index": 元素下标, "action": "replace"|"delete"|"insert_after"|"replace_text", ...}
   - 批量参数: {"edits": [{"index": ..., "action": ...}, ...]}
   - action 说明：
     * replace: 替换该元素内容，需提供 paragraph 或 table 字段；
     * delete: 删除该元素；
     * insert_after: 在该元素后插入新段落，需提供 paragraph 或 paragraphs 字段；
     * replace_text: 精准替换子串，需提供 old_text 与 new_text 字段。
5. end: 结束改写并应用生效。当所有修改已登记完毕时调用。参数为空字典 {}。"""

MAX_DIRECT_TOKENS = 6000 #估算token低于该值的文档直接整篇差量改写（约5000~6000字，一轮完成规划）；超长文档才工具化按需改写
MAX_TOOL_ROUNDS = 20 #工具化改写的最大轮数
FEEDBACK_MAX_ROUNDS = 4 #放进prompt的最近轮数，避免历史工具反馈把上下文越撑越大
FEEDBACK_MAX_CHARS_PER_ROUND = 600 #单轮工具结果最多保留的字符数
MAX_SECTION_TOKENS = 4000 #全局重塑时单次送给模型的章节最大估算token，超过就按元素边界再分块
SEARCH_MAX_HITS = 20 #search工具最多返回的命中元素数
SEARCH_MAX_TERMS = 5 #search工具最多拆分的组合关键词数
SEARCH_MAX_HITS_PER_ELEMENT = 3 #单个元素内最多返回的命中位置数，避免局部批量改写漏改
MAX_READ_CHARS = 1500 #read工具单次最多返回的字符数，调小避免模型一次把全文读回来
SEARCH_CONTEXT_BEFORE = 50 #search结果中关键词前保留的字符数
SEARCH_CONTEXT_AFTER = 60 #search结果中关键词后保留的字符数
HEADING_MAX_CHARS = 35 #启发式判定标题时的最大字数
HEADING_MIN_FONT_SIZE = 14 #启发式判定标题时的大字号阈值
VIRTUAL_BLOCK_SIZE = 18 #无标题文档按多少个元素聚合成一个虚拟块
BLOCK_PREVIEW_CHARS = 40 #虚拟块首尾句保留的字符数

_P_CHAPTER = [
    re.compile(r"^第[一二三四五六七八九十百千0-9]+[编篇章部分卷]"),
    re.compile(r"^(附录|附件)[A-Za-z0-9一二三四五六七八九十]+"),
    re.compile(r"^(Chapter|Part)\s+\d+", re.IGNORECASE),
    re.compile(r"^【.+?】"),
]
_P_SECTION = [
    re.compile(r"^第[一二三四五六七八九十百千0-9]+[节条]"),
    re.compile(r"^Section\s+\d+", re.IGNORECASE),
]
_P_CN_NUM = re.compile(r"^[一二三四五六七八九十百]+[、.．]")
_P_CN_PAREN = [
    re.compile(r"^（[一二三四五六七八九十0-9]+）"),
    re.compile(r"^\([一二三四五六七八九十0-9]+\)"),
]
_P_ARABIC_1 = [
    re.compile(r"^\d+\.(?!\d)\s*\S"),
    re.compile(r"^\d+[、．]\s*\S"),
    re.compile(r"^\d+\s+[^\d\s]"),
]
_P_ARABIC_2 = re.compile(r"^\d+\.\d+(?!\.\d)(\s+|[、.．]|\b)")
_P_ARABIC_3 = re.compile(r"^\d+\.\d+\.\d+(?!\.\d)(\s+|[、.．]|\b)")
_P_ARABIC_4 = re.compile(r"^\d+\.\d+\.\d+\.\d+(\s+|[、.．]|\b)")
_P_ARABIC_PAREN = [
    re.compile(r"^（\d+）"),
    re.compile(r"^\(\d+\)"),
]

def _build_patterns(level_map: Dict[int, list]) -> List[Tuple[int, re.Pattern]]:
    """根据层级映射快速构造 (level, pattern) 规则列表。"""
    patterns = []
    for level, items in sorted(level_map.items()):
        for item in items:
            if isinstance(item, list):
                for p in item:
                    patterns.append((level, p))
            else:
                patterns.append((level, item))
    return patterns

# 默认通用体例规则
DEFAULT_HEADING_PATTERNS = _build_patterns({
    1: [_P_CHAPTER, _P_ARABIC_1],
    2: [_P_SECTION, [_P_CN_NUM, _P_ARABIC_2]],
    3: [_P_CN_PAREN, [_P_ARABIC_3, _P_ARABIC_1[0], _P_ARABIC_1[1]]],
    4: [_P_ARABIC_PAREN, [_P_ARABIC_4]],
})


def get_heading_patterns_for_doc(nodes_or_texts=None) -> List[Tuple[int, re.Pattern]]:
    has_tech_dotted = False
    has_cn_num = False
    has_chapter = False

    if nodes_or_texts:
        for item in nodes_or_texts:
            text = (item.text if hasattr(item, "text") else str(item)).strip()
            if not text or len(text) > HEADING_MAX_CHARS:
                continue
            if _P_ARABIC_2.match(text):
                has_tech_dotted = True
            if _P_CN_NUM.match(text):
                has_cn_num = True
            if any(p.match(text) for p in _P_CHAPTER):
                has_chapter = True

    # 1. 科技/标准规范体例 (GB/T 1.1 / GB/T 7713)：存在 1.1 / 1.1.1
    if has_tech_dotted:
        return _build_patterns({
            1: [_P_CHAPTER, _P_ARABIC_1],
            2: [_P_SECTION, [_P_ARABIC_2, _P_CN_NUM]],
            3: [[_P_ARABIC_3], _P_CN_PAREN],
            4: [[_P_ARABIC_4], _P_ARABIC_PAREN],
        })

    # 2. 篇章规章体例（有第一章、第一节等大章节）+ 中文序号
    if has_chapter:
        return _build_patterns({
            1: [_P_CHAPTER],
            2: [_P_SECTION, [_P_CN_NUM, _P_ARABIC_2]],
            3: [_P_CN_PAREN, [_P_ARABIC_3, _P_ARABIC_1[0], _P_ARABIC_1[1]]],
            4: [_P_ARABIC_PAREN, [_P_ARABIC_4]],
        })

    # 3. 标准党政机关公文体例 (GB/T 9704-2012)：以“一、”作为第一层，“（一）”作为第二层，“1.”作为第三层，“（1）”作为第四层
    if has_cn_num:
        return _build_patterns({
            1: [[_P_CN_NUM], _P_CHAPTER],
            2: [_P_CN_PAREN, _P_SECTION],
            3: [[_P_ARABIC_1[0], _P_ARABIC_1[1], _P_ARABIC_2]],
            4: [_P_ARABIC_PAREN, [re.compile(r"^\d+\.\d+\.\d+(\.(?!\d)|\s+|[、.．]|\b)")]],
        })

    # 4. 默认通用体例
    return DEFAULT_HEADING_PATTERNS


class ParagraphEdit(BaseModel):#对文档中单个元素的修改
    index: int = Field(description="要修改的元素下标，与待改写文档结构中的index一致")
    action: str = Field(description="修改动作，replace表示替换该元素内容，delete表示删除该元素，insert_after表示在该元素之后插入一个新段落")
    paragraph: Optional[ParagraphItem] = Field(default=None, description="action为replace或insert_after时，该段落改写后或新增的内容")
    paragraphs: Optional[List[ParagraphItem]] = Field(default=None, description="action为insert_after且需要一次插入多个段落时使用，按列表顺序插入；与paragraph二选一")
    old_text: Optional[str] = Field(default=None, description="action为replace_text时，要被替换的原文片段（必须与原文完全一致）")
    new_text: Optional[str] = Field(default=None, description="action为replace_text时，替换后的新文本")
    table: Optional[TableItem] = Field(default=None, description="当被修改的元素是表格且需要替换整张表格内容时使用该字段")
    reason: str = Field(default="", description="本次修改的原因")

    @model_validator(mode="before")
    @classmethod
    def _coerce_edit(cls, data):
        if isinstance(data, dict):
            action = str(data.get("action") or "").strip().lower()
            if action in ("insert", "add", "append"):
                action = "insert_after"
                data["action"] = action
            elif action in ("del", "remove"):
                action = "delete"
                data["action"] = action

            if "index" in data and not isinstance(data["index"], int):
                try:
                    data["index"] = int(data["index"])
                except (ValueError, TypeError):
                    pass

            # 兼容模型直接给出 text / content / new_content
            if "paragraph" not in data and "paragraphs" not in data:
                for key in ("content", "text", "new_content", "new_paragraph"):
                    if key in data and data[key]:
                        val = data[key]
                        if isinstance(val, str):
                            lines = [l.strip() for l in val.split("\n") if l.strip()]
                            if len(lines) > 1:
                                data["paragraphs"] = lines
                            elif lines:
                                data["paragraph"] = lines[0]
                        elif isinstance(val, list):
                            data["paragraphs"] = val
                        elif isinstance(val, dict):
                            data["paragraph"] = val
                        break

            # 兼容 paragraphs 传了包含换行的长字符串
            if "paragraphs" in data and isinstance(data["paragraphs"], str):
                lines = [l.strip() for l in data["paragraphs"].split("\n") if l.strip()]
                data["paragraphs"] = lines

            # 兼容 paragraph 传了包含换行的长字符串
            if "paragraph" in data and isinstance(data["paragraph"], str) and "\n" in data["paragraph"]:
                lines = [l.strip() for l in data["paragraph"].split("\n") if l.strip()]
                if len(lines) > 1:
                    data["paragraphs"] = lines
                    data["paragraph"] = None
        return data


class RewritePlan(BaseModel):#一次改写的完整方案
    summary: str = Field(default="", description="本次改写的整体思路")
    edits: List[ParagraphEdit] = Field(default_factory=list, description="对文档的具体修改列表，没有需要修改的元素时为空列表")

    @model_validator(mode="before")
    @classmethod
    def _coerce_plan(cls, data):
        if isinstance(data, list):
            return {"summary": "批量修改方案", "edits": data}
        if isinstance(data, dict):
            if "edits" not in data:
                for key in ("items", "modifications", "actions", "plan", "changes"):
                    if key in data and isinstance(data[key], list):
                        data["edits"] = data[key]
                        break
        return data

class RewriteToolCall(BaseModel):#改写过程中模型选择要调用的工具
    reason: str = Field(default="", description="选择该工具的原因与下一步思路")
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
    if run.font_name:
        result["font_name"] = run.font_name
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


def _estimate_tokens(text: str) -> int:#粗略估算token：中日韩等全角字符约1token/字，其余字符约4字符/token
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - wide
    return wide + (other + 3) // 4


def _estimate_description_tokens(description) -> int:#用序列化后的JSON估算，计入index/type/runs/格式字段等结构开销
    return _estimate_tokens(json.dumps(description, ensure_ascii=False))


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


def _detect_heading(node, base_item, text, patterns=None):
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
    rule_patterns = patterns if patterns is not None else DEFAULT_HEADING_PATTERNS
    for level, pattern in rule_patterns:
        if pattern.match(text):
            return level, text
    if _looks_like_visual_heading(base_item):
        return 2, text
    return None


# 识别落款/结尾元数据的正则表达式与关键词，这个部分可以根据后续知识库补充之后修改
TAIL_DATE_PATTERN = re.compile(r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-./]\d{1,2}[-./]\d{1,2})")
TAIL_ORG_SUFFIXES = ("办公室", "部", "委员会", "局", "处", "科", "组", "公室", "公司", "院", "中心", "学会", "协会")
TAIL_SPECIAL_KEYWORDS = ("抄报", "抄送", "印发", "签发", "分发", "记录人", "记录：", "主持人", "主持：", "出席：", "列席：", "特此纪要", "特此通知", "特此报告")


def _detect_paragraph_role(node, base_item, text: str, index: int, total_elements: int, patterns=None) -> str:
    if not isinstance(node, Paragraph):
        return "table" if isinstance(node, Table) else "unknown"
    if not text:
        return "body"

    # 1. 优先识别各级标题
    if _detect_heading(node, base_item, text, patterns=patterns) is not None:
        return "heading"

    # 2. 识别文档末尾的落款/日期元数据（限制在文档最后 6 个元素内）
    if total_elements > 0 and index >= max(0, total_elements - 6):
        # 日期模式（如 "2026年9月18日" 或 "2026.09.18"）
        if len(text) <= 30 and TAIL_DATE_PATTERN.search(text):
            return "tail_meta"
        # 居右对齐的短文本（公文经典落款特征）
        align = getattr(base_item, "alignment", None) or ""
        if str(align).lower() in ("right", "end") and len(text) <= 50:
            return "tail_meta"
        # 常见单位署名或落款后缀
        if len(text) <= 35 and any(text.endswith(s) for s in TAIL_ORG_SUFFIXES):
            return "tail_meta"
        # 常见公文抄送/记录等关键词
        if len(text) <= 40 and any(kw in text for kw in TAIL_SPECIAL_KEYWORDS):
            return "tail_meta"

    return "body"


def extract_body_profile(base_items) -> dict:
    alignments = []
    font_names = []
    font_sizes = []
    font_colors = []
    line_spacings = []
    spacing_befores = []
    spacing_afters = []
    first_line_indents = []

    for item in base_items or []:
        if isinstance(item, ParagraphItem) and item.runs:
            first_run = item.runs[0]
            text = "".join(r.text for r in item.runs).strip()
            # 过滤掉标题（加粗）、极短文本（如标点或单字）、居右对齐的落款
            if first_run.bold:
                continue
            if len(text) < 10:
                continue
            if item.alignment in ("right", "end"):
                continue

            if item.alignment:
                alignments.append(item.alignment)
            if first_run.font_name:
                font_names.append(first_run.font_name)
            if first_run.font_size and 8 <= first_run.font_size <= 24:
                font_sizes.append(first_run.font_size)
            if first_run.font_color and first_run.font_color.startswith("#"):
                font_colors.append(first_run.font_color)
            if item.line_spacing is not None:
                line_spacings.append(item.line_spacing)
            if item.spacing_before is not None:
                spacing_befores.append(item.spacing_before)
            if item.spacing_after is not None:
                spacing_afters.append(item.spacing_after)
            if item.first_line_indent is not None:
                first_line_indents.append(item.first_line_indent)

    default_align = max(set(alignments), key=alignments.count) if alignments else "left"
    default_fn = max(set(font_names), key=font_names.count) if font_names else None
    default_size = max(set(font_sizes), key=font_sizes.count) if font_sizes else DEFAULT_FONT_SIZE
    default_color = max(set(font_colors), key=font_colors.count) if font_colors else DEFAULT_FONT_COLOR
    default_ls = max(set(line_spacings), key=line_spacings.count) if line_spacings else 1.25
    default_sb = max(set(spacing_befores), key=spacing_befores.count) if spacing_befores else 0
    default_sa = max(set(spacing_afters), key=spacing_afters.count) if spacing_afters else 0
    default_fli = max(set(first_line_indents), key=first_line_indents.count) if first_line_indents else None

    return {
        "alignment": default_align,
        "font_name": default_fn,
        "font_size": default_size,
        "font_color": default_color,
        "line_spacing": default_ls,
        "spacing_before": default_sb,
        "spacing_after": default_sa,
        "first_line_indent": default_fli,
    }


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


def _section_preview(description, start: int, end: int, max_chars: int = BLOCK_PREVIEW_CHARS) -> str:
    #取区间内第一个有正文的元素作为章节预览（跳过空元素），供outline返回首句线索
    for i in range(max(0, start), min(len(description), end + 1)):
        text = _item_text(description[i]).strip()
        if text:
            return text[:max_chars]
    return ""


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
            "preview": _section_preview(description, section["start"], section["end"]),
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
        head_text = _item_text(items[0])[:BLOCK_PREVIEW_CHARS]
        tail_text = _item_text(items[-1])[:BLOCK_PREVIEW_CHARS]
        blocks.append({
            "title": f"第{len(blocks)+1}部分 ({head_text}...)",
            "index_range": [start, end],
            "start": start,
            "end": end,
            "stats": _range_stats(description, start, end),
            "head": head_text,
            "tail": tail_text,
        })
    return blocks


def build_outline_tree(nodes, description, base_items) -> dict:
    total = len(description)
    summary = _range_stats(description, 0, total - 1) if total else {
        "elements": 0, "paragraphs": 0, "tables": 0, "chars": 0}
    patterns = get_heading_patterns_for_doc(nodes)
    raw_sections = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Paragraph):
            continue
        detected = _detect_heading(node, base_items[index], (node.text or "").strip(), patterns=patterns)
        if detected is None:
            continue
        level, title = detected
        raw_sections.append({"level": level, "title": title[:60], "start": index})

    if len(raw_sections) >= 2:
        # 1. 动态层级归一化：若文档未出现“第1章”而以“一、”（通常归类为2）作为顶层，平移使得最高标题为 Level 1
        min_level = min(s["level"] for s in raw_sections)
        if min_level > 1:
            for s in raw_sections:
                s["level"] = max(1, s["level"] - (min_level - 1))

        # 2. 如果文档开头到第一个标题之间有正文，作为前言/开头
        if raw_sections[0]["start"] > 0:
            raw_sections.insert(0, {"level": 1, "title": "（文档开头/前言）", "start": 0})

        # 3. 严格计算父子区间的完整覆盖：
        # 一个章节的 end 是紧随其后、层级 <= 当前章节层级的下一个标题的 start - 1（若无则到文档末尾）
        for i, s in enumerate(raw_sections):
            end = total - 1
            for j in range(i + 1, len(raw_sections)):
                if raw_sections[j]["level"] <= s["level"]:
                    end = raw_sections[j]["start"] - 1
                    break
            s["end"] = end

        # 4. 构建层级树（嵌套 children）
        tree_entries = _to_tree_entries(raw_sections, description)
        summary["mode"] = "heading_tree"
        summary["section_count"] = len(raw_sections)
        summary["major_section_count"] = len(tree_entries)
        return {"summary": summary, "outline": tree_entries}

    summary["mode"] = "virtual_blocks"
    summary["block_size"] = VIRTUAL_BLOCK_SIZE
    return {"summary": summary, "outline": _build_virtual_blocks(description)}


def _format_outline_text(outline_tree: dict) -> str:
    summary = outline_tree.get("summary", {})
    mode = summary.get("mode")
    outline = outline_tree.get("outline", [])
    if not outline:
        return "文档大纲：空文档"

    lines = [
        f"【文档大纲】共 {summary.get('elements', 0)} 个元素（{summary.get('paragraphs', 0)}段落、{summary.get('tables', 0)}表格、约{summary.get('chars', 0)}字）："
    ]

    def _render_entries(entries, prefix=""):
        for i, entry in enumerate(entries, 1):
            num = f"{prefix}{i}" if prefix else f"{i}"
            ir = entry.get("index_range", [0, 0])
            title = entry.get("title") or entry.get("head") or "未命名章节"
            st = entry.get("stats", {})
            chars = st.get("chars", 0)
            elem_count = ir[1] - ir[0] + 1
            indent = "  " * (entry.get("level", 1) - 1)
            lines.append(f"{indent}{num}. [{ir[0]}~{ir[1]}] {title} ({elem_count}元素, 约{chars}字)")
            children = entry.get("children", [])
            if children:
                _render_entries(children, prefix=f"{num}.")

    if mode == "virtual_blocks":
        for i, b in enumerate(outline, 1):
            ir = b.get("index_range", [0, 0])
            lines.append(f"{i}. [{ir[0]}~{ir[1]}] 部分：{b.get('head', '')}...")
    else:
        _render_entries(outline)

    return "\n".join(lines)


def _tool_outline(outline_tree, arguments) -> str:
    #返回格式化后的层级文本大纲，紧凑清晰且绝不被截断
    return _format_outline_text(outline_tree)


def _split_terms(query: str) -> list:
    #把query按常见分隔符拆成多个关键词，支持"关键词1 关键词2"这类组合检索（任一命中即返回）
    parts = re.split(r"[\s,，、;；|/]+", query)
    return [p.strip() for p in parts if p.strip()][:SEARCH_MAX_TERMS]


def _search_in_table(item, terms) -> Optional[dict]:
    #表格命中：返回表头与命中的单元格，避免把整张表拼成一段乱序文本
    cells = item.get("cells", [])
    header = cells[0] if cells else []
    hits = []
    for row_index, row in enumerate(cells):
        for col_index, cell in enumerate(row):
            cell_text = str(cell)
            lowered = cell_text.lower()
            for term in terms:
                position = lowered.find(term.lower())
                if position < 0:
                    continue
                hits.append({
                    "term": term,
                    "row": row_index,
                    "col": col_index,
                    "context": _context_window(cell_text, position, len(term)),
                })
                if len(hits) >= SEARCH_MAX_HITS_PER_ELEMENT:
                    break
            if len(hits) >= SEARCH_MAX_HITS_PER_ELEMENT:
                break
        if len(hits) >= SEARCH_MAX_HITS_PER_ELEMENT:
            break
    if not hits:
        return None
    return {
        "index": item["index"],
        "type": "table",
        "header": header,
        "hit_count": len(hits),
        "hits": hits,
    }


def _search_in_paragraph(item, terms) -> Optional[dict]:
    #段落命中：报告全部命中位置（最多 SEARCH_MAX_HITS_PER_ELEMENT 处），避免局部批量改写漏改
    text = _item_text(item)
    if not text:
        return None
    lowered = text.lower()
    hits = []
    for term in terms:
        term_lower = term.lower()
        start = 0
        while len(hits) < SEARCH_MAX_HITS_PER_ELEMENT:
            position = lowered.find(term_lower, start)
            if position < 0:
                break
            hits.append({"term": term, "context": _context_window(text, position, len(term))})
            start = position + len(term_lower)
    if not hits:
        return None
    return {
        "index": item["index"],
        "type": "paragraph",
        "matched_terms": sorted({hit["term"] for hit in hits}),
        "hit_count": len(hits),
        "hits": hits,
    }


def _tool_search(description, arguments) -> str:
    #按关键词搜索文档内容，返回命中元素的全局下标与关键词前后文（支持多个关键词组合）
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "search工具反馈：query不能为空"
    terms = _split_terms(query)
    if not terms:
        return "search工具反馈：query不能为空"
    hits = []
    for item in description:
        if len(hits) >= SEARCH_MAX_HITS:
            break
        if item.get("type") == "table":
            hit = _search_in_table(item, terms)
        else:
            hit = _search_in_paragraph(item, terms)
        if hit:
            hits.append(hit)
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


def _record_single_edit(description, edit_dict, edited) -> str:
    try:
        edit = ParagraphEdit.model_validate(edit_dict)
    except Exception as e:
        return f"edit失败：修改内容不合法（{e}）"
    if edit.index < 0 or edit.index >= len(description):
        return f"edit失败：下标{edit.index}越界，文档元素的下标范围是0~{len(description) - 1}"
    action = (edit.action or "").strip().lower()
    if action not in ("replace", "delete", "insert_after", "replace_text"):
        return f"edit失败：不支持的动作{edit.action}，只支持replace/delete/insert_after/replace_text"
    if action == "replace" and edit.paragraph is None and edit.table is None:
        return "edit失败：replace必须提供paragraph（段落）或table（表格）字段"
    if action == "insert_after" and edit.paragraph is None and not edit.paragraphs:
        return "edit失败：insert_after必须提供paragraph（单段）或paragraphs（多段）字段"
    if action == "replace_text" and not (edit.old_text or "").strip():
        return "edit失败：replace_text必须提供old_text字段（要替换的原文片段）"

    for position, existing in enumerate(edited):
        if existing.index == edit.index and (existing.action or "").strip().lower() == action:
            edited[position] = edit
            return (
                f"edit工具反馈：已更新对下标{edit.index}的{action}修改（当前队列共{len(edited)}条）。"
                f"【注意】：该修改已被成功暂存，请不要反复提交相同修改！若规划完毕请立即调用 end 结束！"
            )
    edited.append(edit)
    return (
        f"edit工具反馈：已成功记录对下标{edit.index}的{action}修改（当前队列共{len(edited)}条）。"
        f"【注意】：read读到的是原文档快照不会改变；若您已完成所有修改的登记，请立即调用 end 结束改写！"
    )


def _tool_edit(description, arguments, edited) -> str:
    # 校验并记录一条或多条修改（支持单条参数或批量 edits 参数）
    if isinstance(arguments, dict) and "edits" in arguments and isinstance(arguments["edits"], list):
        edits_list = arguments["edits"]
        if not edits_list:
            return "edit工具反馈：传入的 edits 列表为空"
        results = []
        for single in edits_list:
            if isinstance(single, dict):
                results.append(_record_single_edit(description, single, edited))
        feedback_details = "\n".join(results)
        return (
            f"edit工具反馈：批量处理完成（共成功暂存 {len(edited)} 项修改）：\n"
            f"{feedback_details}\n"
            f"【注意】：若已完成全部改写规划，请立即调用 end 工具结束改写并应用生效！"
        )
    return _record_single_edit(description, arguments, edited)


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


def _clip_feedback(text: str, limit: int = FEEDBACK_MAX_CHARS_PER_ROUND) -> str:
    #单轮工具结果最多保留limit个字符，超长时截断并提示模型按更小范围重新读取
    if len(text) <= limit:
        return text
    return f"{text[:limit]}……(已截断，原文共{len(text)}字，如需完整内容请read更小的范围)"


def _rewrite_with_tools(llm, dialog, nodes, description, base_items):
    #工具化改写：先构建大纲，再由模型按需探查文档并逐条提交修改，返回(修改列表, 错误信息)
    outline_tree = build_outline_tree(nodes, description, base_items)
    summary = outline_tree.get("summary", {})
    print(f"文档大纲模式：{summary.get('mode')}，元素{summary.get('elements')}个"
          f"（段落{summary.get('paragraphs')}、表格{summary.get('tables')}、约{summary.get('chars')}字）")
    for entry in outline_tree.get("outline", [])[:10]:
        print("   ", entry.get("title") or entry.get("head") or "", entry.get("index_range"))
    edited = []
    history = []
    for _ in range(MAX_TOOL_ROUNDS):
        #只把最近 FEEDBACK_MAX_ROUNDS 轮的结果放进prompt，避免历史反馈线性膨胀把上下文撑爆
        feedback = "\n".join(history[-FEEDBACK_MAX_ROUNDS:])
        prompt = (
            "你是文档改写助手。不要把整篇文档读进来，而是用工具按需探查和修改。\n"
            f"{TOOL_DESCRIPTION}\n"
            f"用户对话内容：{dialog}\n"
            f"{feedback}\n"
            f"【状态提示】当前待执行队列中已暂存的修改数量：{len(edited)}条。\n"
            + ("（提示：您已暂存了修改，若已登记完所有修改项，请直接调用 end 工具提交生效！）\n" if edited else "")
            + "请先在reason里简述下一步思路，再选择并调用一个工具。"
        )
        tool = llm_model_invoke(llm, prompt, RewriteToolCall)
        if tool is None:
            history.append("工具调用失败，请重新选择工具")
            continue
        if tool.reason:
            print(f"改写思路：{tool.reason}")
        result = _execute_tool(tool, description, outline_tree, edited)
        print(f"改写工具：{tool.tool_name} {tool.arguments} -> {result[:200]}")
        history.append(f"调用{tool.tool_name}({tool.arguments}) -> {_clip_feedback(result)}")
        if (tool.tool_name or "").strip().lower() == "end":
            return edited, ""
    return edited, f"改写超过{MAX_TOOL_ROUNDS}轮仍未结束，请把改写要求拆成更小的任务"


def _table_merge_info(table) -> list:
    """提取表格的合并单元格信息，供模型重写表格时用 row_span/col_span 保持合并结构。"""
    merges = []
    try:
        rows = table.rows
        seen = set()
        for row_index, row in enumerate(rows):
            for col_index, cell in enumerate(row.cells):
                tc = cell._tc
                if id(tc) in seen:
                    continue  #横向合并时同一个 tc 会被重复返回，只报告一次
                seen.add(id(tc))
                tc_pr = tc.tcPr
                if tc_pr is None:
                    continue
                v_merge = tc_pr.find(qn("w:vMerge"))
                if v_merge is not None and (v_merge.get(qn("w:val")) or "continue") != "restart":
                    continue  #被上方合并覆盖的单元格，不单独报告
                col_span = 1
                grid_span = tc_pr.find(qn("w:gridSpan"))
                if grid_span is not None:
                    try:
                        col_span = int(grid_span.get(qn("w:val")) or 1)
                    except (TypeError, ValueError):
                        col_span = 1
                row_span = 1
                if v_merge is not None:
                    #统计紧随其下、同列的 vMerge=continue 行数，得到纵向合并跨度
                    for below in range(row_index + 1, len(rows)):
                        try:
                            below_tc = rows[below].cells[col_index]._tc
                        except IndexError:
                            break
                        below_pr = below_tc.tcPr
                        below_v = below_pr.find(qn("w:vMerge")) if below_pr is not None else None
                        if below_v is None or (below_v.get(qn("w:val")) or "continue") == "restart":
                            break
                        row_span += 1
                if row_span > 1 or col_span > 1:
                    merges.append({
                        "row": row_index,
                        "col": col_index,
                        "row_span": row_span,
                        "col_span": col_span,
                    })
    except Exception:
        return []
    return merges


def build_document_description(doc: Document):
    #解析文档的每个元素，使得改写的时候能够准确定位
    nodes = list(doc.iter_inner_content())
    total = len(nodes)
    patterns = get_heading_patterns_for_doc(nodes)
    base_items = []
    description = []
    section_stack = []  # 记录当前所处的标题层级栈 [(level, title)]

    for index, node in enumerate(nodes):
        if isinstance(node, Paragraph):
            try:
                #preserve_none=True：未直接设置的格式保留为None，
                #改写时才能区分"原文没设置（由样式决定）"和"原文显式写了默认值"
                item = convert_node(node, preserve_none=True)
            except Exception as e:
                print(f"解析第{index}个段落失败，按纯文本处理：{e}")
                item = ParagraphItem(runs=[TextRunItem(text=node.text, text_type="text")])
            base_items.append(item)
            para_dict = _paragraph_to_dict(item)
            if paragraph_has_protected_content(node):
                #让模型知道该段含图片等无法表达的内容，改写时会保留这些元素、只替换文字
                para_dict["has_media"] = True

            text_strip = (node.text or "").strip()
            role = _detect_paragraph_role(node, item, text_strip, index, total, patterns=patterns)
            heading_info = _detect_heading(node, item, text_strip, patterns=patterns) if role == "heading" else None

            # 维护父子层级路径
            if heading_info:
                h_level, h_title = heading_info
                while section_stack and section_stack[-1][0] >= h_level:
                    section_stack.pop()
                section_stack.append((h_level, h_title))
                para_dict["heading_level"] = h_level

            section_path = "/".join(s[1] for s in section_stack) if section_stack else ""
            if section_path:
                para_dict["section_path"] = section_path

            description.append({"index": index, "type": "paragraph", "role": role, **para_dict})
        elif isinstance(node, Table):
            base_items.append(None)
            cells = [[cell.text for cell in row.cells] for row in node.rows]
            table_dict = {
                "index": index,
                "type": "table",
                "role": "table",
                "rows": len(cells),
                "cols": len(cells[0]) if cells else 0,
                "cells": cells,
            }
            if section_stack:
                table_dict["section_path"] = "/".join(s[1] for s in section_stack)
            merges = _table_merge_info(node)
            if merges:
                #告知模型合并单元格的位置与跨度，重写表格时用row_span/col_span保持合并结构
                table_dict["merges"] = merges
            description.append(table_dict)
        else:
            base_items.append(None)
            unk_dict = {"index": index, "type": "unknown", "role": "unknown"}
            if section_stack:
                unk_dict["section_path"] = "/".join(s[1] for s in section_stack)
            description.append(unk_dict)
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
        font_name=base_run.font_name if new_run.font_name is None else new_run.font_name,
        font_size=base_run.font_size if new_run.font_size is None else new_run.font_size,
        font_color=base_run.font_color if new_run.font_color is None else new_run.font_color,
    )


def _inherit_paragraph_format(new_item: ParagraphItem, base_item,
                             body_profile: Optional[dict] = None,
                             is_insert: bool = False) -> ParagraphItem:
    """让改写后的段落继承合适的格式。

    - is_insert 为 True 时（新增段落）：优先使用文档全局正文基准格式（body_profile），
      避免盲目继承前一个锚点元素（如标题或落款）的加粗、大字号或居右对齐。
    - is_insert 为 False 时（替换原段落）：继承原段落 base_item 的格式，保持原位排版一致。
    - 模型显式给出的格式始终具有最高优先级。
    """
    profile = body_profile or {
        "alignment": "left",
        "font_name": "宋体",
        "font_size": DEFAULT_FONT_SIZE,
        "font_color": DEFAULT_FONT_COLOR,
        "line_spacing": 1.25,
        "spacing_before": 0,
        "spacing_after": 0,
        "first_line_indent": None,
    }

    if is_insert:
        # 新增段落：使用正文基准格式，模型显式指定优先
        alignment = new_item.alignment if new_item.alignment is not None else profile.get("alignment", "left")
        spacing_before = new_item.spacing_before if new_item.spacing_before is not None else profile.get("spacing_before", 0)
        spacing_after = new_item.spacing_after if new_item.spacing_after is not None else profile.get("spacing_after", 0)
        line_spacing = new_item.line_spacing if new_item.line_spacing is not None else profile.get("line_spacing", 1.25)
        first_line_indent = new_item.first_line_indent if new_item.first_line_indent is not None else profile.get("first_line_indent")

        new_runs = []
        for run in new_item.runs:
            fname = run.font_name if run.font_name is not None else profile.get("font_name")
            fsize = run.font_size if run.font_size is not None else profile.get("font_size", DEFAULT_FONT_SIZE)
            fcolor = run.font_color if run.font_color is not None else profile.get("font_color", DEFAULT_FONT_COLOR)
            bold = run.bold if run.bold is not None else False
            italic = run.italic if run.italic is not None else False
            underline = run.underline if run.underline is not None else False
            new_runs.append(TextRunItem(
                text=run.text,
                text_type=run.text_type,
                bold=bold,
                italic=italic,
                underline=underline,
                font_name=fname,
                font_size=fsize,
                font_color=fcolor,
            ))
        return ParagraphItem(
            runs=new_runs,
            alignment=alignment,
            spacing_before=spacing_before,
            spacing_after=spacing_after,
            line_spacing=line_spacing,
            first_line_indent=first_line_indent,
        )

    # is_insert 为 False：替换原段落（尽量继承原段落格式）
    if not isinstance(base_item, ParagraphItem) or not base_item.runs:
        return new_item
    base_run = base_item.runs[0]
    return ParagraphItem(
        runs=[_inherit_run_format(run, base_run) for run in new_item.runs],
        alignment=base_item.alignment if new_item.alignment is None else new_item.alignment,
        spacing_before=base_item.spacing_before if new_item.spacing_before is None else new_item.spacing_before,
        spacing_after=base_item.spacing_after if new_item.spacing_after is None else new_item.spacing_after,
        line_spacing=base_item.line_spacing if new_item.line_spacing is None else new_item.line_spacing,
        first_line_indent=base_item.first_line_indent if new_item.first_line_indent is None else new_item.first_line_indent,
        left_indent=base_item.left_indent if new_item.left_indent is None else new_item.left_indent,
        right_indent=base_item.right_indent if new_item.right_indent is None else new_item.right_indent,
    )


def apply_rewrite_plan(doc: Document, nodes, base_items, edits, description=None) -> int:
    #修改按下标从后往前执行，避免插入/删除导致后续下标错位；同一下标只处理第一条修改。
    applied = 0
    body_profile = extract_body_profile(base_items)

    # 扫描文档末尾连续的 tail_meta（落款/日期等），定位落款起始下标
    tail_start_index = len(nodes)
    for i in range(len(nodes) - 1, -1, -1):
        role = None
        if description and i < len(description):
            role = description[i].get("role")
        if not role and i < len(nodes):
            node = nodes[i]
            text = (node.text or "").strip() if isinstance(node, Paragraph) else ""
            role = _detect_paragraph_role(node, base_items[i], text, i, len(nodes))
        if role == "tail_meta":
            tail_start_index = i
        else:
            break

    # 同一下标允许同时"替换"和"追加"，因此按动作优先级排序；下标从大到小处理，避免增删导致下标错位
    action_priority = {"replace": 0, "replace_text": 0, "insert_after": 1, "delete": 2}
    ordered_edits = sorted(
        edits or [],
        key=lambda item: (-item.index, action_priority.get((item.action or "").strip().lower(), 9)),
    )
    # 同一下标同一动作只保留最后一次提交（后提交覆盖先提交，给模型自我纠错的机会）
    last_positions = {}
    for position, item in enumerate(ordered_edits):
        last_positions[(item.index, (item.action or "").strip().lower())] = position
    ordered_edits = [
        item for position, item in enumerate(ordered_edits)
        if last_positions[(item.index, (item.action or "").strip().lower())] == position
    ]
    for edit in ordered_edits:
        action = (edit.action or "").strip().lower()
        # 处理负数索引（如 -1 表示最后一个元素）
        if edit.index < 0 and len(nodes) > 0:
            edit.index = edit.index + len(nodes)
        # 处理在文档末尾追加（如 index >= len(nodes) 且 action 是 insert_after）
        if action == "insert_after" and edit.index >= len(nodes) and len(nodes) > 0:
            edit.index = len(nodes) - 1

        # 【解法三：执行层智能纠偏】
        # 若在落款（tail_start_index）及其之后做 insert_after，自动前移至落款之前（tail_start_index - 1）
        if action == "insert_after" and edit.index >= tail_start_index and tail_start_index > 0:
            target_index = tail_start_index - 1
            print(f"[智能纠偏] 检测到在末尾落款/日期（第{edit.index}个元素）后插入内容，已自动前移至正文末尾（第{target_index}个元素后）插入，以保持公文版式规范。")
            edit.index = target_index

        if edit.index < 0 or edit.index >= len(nodes):
            print(f"忽略越界的修改下标：{edit.index}")
            continue
        node = nodes[edit.index]
        base_item = base_items[edit.index]
        try:
            if action == "delete":
                if element_has_protected_content(node._element):
                    print(f"第{edit.index}个元素含图片等无法表达的内容，为避免误删已跳过删除（如需删除请手动处理）")
                    continue
                node._element.getparent().remove(node._element)
                applied += 1
                print(f"已删除第{edit.index}个元素")
            elif action == "replace":
                if isinstance(node, Table):
                    if not isinstance(edit.table, TableItem) or edit.table.rows <= 0 or edit.table.cols <= 0 or not edit.table.grid:
                        print(f"第{edit.index}个元素是表格，但模型未提供有效的表格内容(rows>0, cols>0, grid非空)，跳过替换以保留原表格")
                        continue
                    #表格替换会重建表格对象，必须把新对象写回nodes，
                    #否则同一下标的insert_after会拿到已被移除的旧表格元素，插入被静默丢弃
                    if element_has_protected_content(node._element):
                        print(f"第{edit.index}个表格含图片等无法表达的内容，为避免丢失已跳过替换表格")
                        continue
                    node = replace_node_with_data(node, edit.table, doc, write_defaults=False)
                    nodes[edit.index] = node
                    applied += 1
                else:
                    replace_items = list(edit.paragraphs or [])
                    if not replace_items and isinstance(edit.paragraph, ParagraphItem):
                        replace_items = [edit.paragraph]
                    if not replace_items:
                        print(f"第{edit.index}个元素是段落，但模型没有给出段落内容，跳过")
                        continue
                    # 第一段原地替换当前 node（is_insert=False，继承原段落格式）
                    replace_node_with_data(node, _inherit_paragraph_format(replace_items[0], base_item, body_profile, is_insert=False), doc, write_defaults=False)
                    # 如果提供了多个段落，后续段落依次插入到当前段落之后（is_insert=True，使用正文基准格式）
                    curr_elem = node._element
                    for extra_item in replace_items[1:]:
                        extra_p = doc.add_paragraph("")
                        replace_node_with_data(extra_p, _inherit_paragraph_format(extra_item, base_item, body_profile, is_insert=True), write_defaults=False)
                        curr_elem.addnext(extra_p._element)
                        curr_elem = extra_p._element
                    applied += len(replace_items)
                print(f"已改写第{edit.index}个元素")
            elif action == "insert_after":
                if isinstance(edit.table, TableItem):
                    new_table = doc.add_table(rows=edit.table.rows, cols=edit.table.cols)
                    fill_table_from_grid(new_table, edit.table.grid, write_defaults=False)
                    node._element.addnext(new_table._element)
                    applied += 1
                    print(f"已在第{edit.index}个元素后插入新表格")
                    continue
                insert_items = list(edit.paragraphs or [])
                if not insert_items and isinstance(edit.paragraph, ParagraphItem):
                    insert_items = [edit.paragraph]
                if not insert_items:
                    print(f"第{edit.index}个元素的插入内容为空，跳过")
                    continue
                # addnext 是逐个"插到锚点后面"，倒序生成才能让最终顺序与模型给出的一致
                # 新增段落使用 is_insert=True，采用正文基准格式，防止污染
                for item in reversed(insert_items):
                    new_paragraph = doc.add_paragraph("")
                    replace_node_with_data(new_paragraph, _inherit_paragraph_format(item, base_item, body_profile, is_insert=True), write_defaults=False)
                    # add_paragraph 会把段落追加到文档末尾，这里移动到目标元素之后
                    node._element.addnext(new_paragraph._element)
                applied += len(insert_items)
                print(f"已在第{edit.index}个元素后插入{len(insert_items)}个新段落")
            elif action == "replace_text":
                old_text = (edit.old_text or "").strip()
                if not old_text:
                    print(f"第{edit.index}个元素缺少old_text，跳过")
                    continue
                new_text = edit.new_text or ""
                if isinstance(node, Paragraph):
                    count = replace_in_paragraph(node, old_text, new_text)
                elif isinstance(node, Table):
                    count = replace_in_table(node, old_text, new_text)
                else:
                    count = 0
                if count == 0:
                    print(f"第{edit.index}个元素中没有找到“{old_text}”，未做替换")
                    continue
                applied += count
                print(f"已在第{edit.index}个元素中替换{count}处“{old_text}”")
            else:
                print(f"忽略未知的修改动作：{edit.action}")
        except Exception as e:
            print(f"应用第{edit.index}个元素的修改失败：{e}")
    return applied

class ReplacementMapping(BaseModel):
    pairs: Dict[str, str] = Field(
        default_factory=dict,
        description="需要查找替换的词语对映射，key为原词/旧词，value为替换后的新词"
    )


def run_patch_rewrite(llm, dialog: str, retrieved_info: str, doc: Document, nodes, base_items, description) -> Tuple[int, str]:
    estimated_tokens = _estimate_description_tokens(description)
    if estimated_tokens <= MAX_DIRECT_TOKENS:
        print(f"【局部修补】文档约{estimated_tokens} tokens，极小文档，直接整篇差量改写")
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
            "3. action为insert_after时，在指定index元素后插入新内容：若插入单段，用paragraph字段；若需插入多个段落（如补充一份简报/新章节），使用paragraphs列表字段一次性按顺序提供全部段落；\n"
            "4. action为delete时表示删除该元素；\n"
            "5. action为replace_text时，用old_text和new_text进行精准子串替换（保留原格式与图片）；\n"
            "6. 没有特别指明格式时，请沿用原文格式（例如标题加粗、居中），不要无故改变格式；\n"
            "7. 不要改动用户没有要求修改的内容；\n"
            "8. 当需要在文档末尾追加内容（如启示、总结、新条款等）时：若文档末尾存在 role 为 'tail_meta' 的落款/日期/抄送等元素，新增内容应使用 insert_after 插入在正文末尾（即首个 tail_meta 元素之前的正文元素后），切勿插入在落款日期之后。\n"
        )
        plan = llm_model_invoke(llm, prompt, RewritePlan)
        if plan is None:
            return 0, "生成文档改写方案失败"
        print("改写思路：", plan.summary)
        edits = plan.edits
    else:
        print(f"【局部修补】文档约{estimated_tokens} tokens，启用工具化按需改写（不整篇进上下文）")
        edits, error = _rewrite_with_tools(llm, f"{dialog}\n{retrieved_info}", nodes, description, base_items)
        if error:
            return 0, error
        if not edits:
            return 0, "模型没有提交任何修改"

    applied = apply_rewrite_plan(doc, nodes, base_items, edits, description)
    return applied, ""


def run_replace_rewrite(llm, dialog: str, doc: Document, replace_pairs: Optional[Dict[str, str]] = None) -> Tuple[int, str]:

    pairs = dict(replace_pairs or {})
    if not pairs:
        print("【全局替换】未显式传入替换键值对，尝试从用户需求中提取...")
        prompt = (
            "你是一个文档全局查找替换助手。用户希望在文档中进行全局查找替换。\n"
            f"用户指令：{dialog}\n"
            "请准确提取用户要求替换的所有词条对，生成以原词为key、替换后新词为value的字典映射。\n"
            "例如指令为：'把全文的“甲方”换成“委托方”，把“2023年”改成“2026年”'，提取结果为：\n"
            "{\"甲方\": \"委托方\", \"2023年\": \"2026年\"}\n"
            "注意：仅提取替换对，不要编造无关内容。"
        )
        mapping = llm_model_invoke(llm, prompt, ReplacementMapping)
        if mapping and mapping.pairs:
            pairs = mapping.pairs
            print(f"【全局替换】从需求中成功提取到替换对：{pairs}")
        else:
            return 0, "未能从用户需求中识别出需要替换的目标词和新词，请明确说明或使用 --find 与 --replace-with 参数"

    if not pairs:
        return 0, "查找替换对为空"

    stats = batch_replace_in_document(doc, pairs)
    total_replaced = sum(stats.values())
    summary_parts = [f"“{k}” -> “{v}” ({stats.get(k, 0)}处)" for k, v in pairs.items()]
    print(f"【全局替换】完成！共替换 {total_replaced} 处：" + "，".join(summary_parts))
    return total_replaced, ""


def extract_sequential_sections(nodes, description, base_items) -> list:

    total = len(nodes)
    if total == 0:
        return []
    patterns = get_heading_patterns_for_doc(nodes)
    headings = []
    for index, node in enumerate(nodes):
        if isinstance(node, Paragraph):
            detected = _detect_heading(node, base_items[index], (node.text or "").strip(), patterns=patterns)
            if detected:
                headings.append((index, detected[0], detected[1][:60]))

    if len(headings) >= 2:
        sections = []
        if headings[0][0] > 0:
            sections.append({"title": "前言/引言", "start": 0, "end": headings[0][0] - 1})
        for i in range(len(headings)):
            start = headings[i][0]
            end = headings[i + 1][0] - 1 if i + 1 < len(headings) else total - 1
            sections.append({"title": headings[i][2], "start": start, "end": end})
        if _ranges_valid([(s["start"], s["end"]) for s in sections], total):
            return sections

    if headings:
        #只识别出一个标题时，也把它当作语义边界，避免标题被夹在虚拟块中间
        sections = []
        if headings[0][0] > 0:
            sections.append({"title": "前言/引言", "start": 0, "end": headings[0][0] - 1})
        sections.append({"title": headings[0][2], "start": headings[0][0], "end": total - 1})
        if _ranges_valid([(s["start"], s["end"]) for s in sections], total):
            return sections

    # 兜底：按虚拟块切分（复用 _build_virtual_blocks）
    return _build_virtual_blocks(description)


def _split_section_ranges(description, start: int, end: int) -> list:

    ranges = []
    chunk_start = start
    for i in range(start + 1, end + 1):
        if _estimate_description_tokens(description[chunk_start:i + 1]) > MAX_SECTION_TOKENS:
            ranges.append((chunk_start, i - 1))
            chunk_start = i
    ranges.append((chunk_start, end))
    return ranges


def _build_global_brief(llm, dialog: str, sections) -> str:

    try:
        outline_text = json.dumps(
            [{"title": s.get("title"), "index_range": [s.get("start"), s.get("end")]} for s in sections],
            ensure_ascii=False,
        )
        prompt = (
            "你是企业公文重构的统筹助手。下面是文档大纲与用户的总体要求，"
            "请给出一份简短的全局改写纲要，用于约束各章节分别改写时的统一性。\n"
            f"用户总体要求：{dialog}\n"
            f"文档大纲：{outline_text}\n"
            "请输出不超过300字的纲要，包含：全文语气风格、章节编号与称谓口径、关键术语的统一写法、"
            "各章需要保持一致的数据口径。仅输出纲要本身。"
        )
        res = llm_invoke(llm, prompt)
        return str(getattr(res, "content", "") or "").strip()[:600]
    except Exception as e:
        print(f"生成全局改写纲要失败，跳过统一性约束：{e}")
        return ""


def _section_is_risky(nodes, description, start: int, end: int) -> bool:

    for i in range(start, end + 1):
        if i >= len(nodes):
            break
        if description[i].get("type") == "unknown":
            return True
        if element_has_protected_content(nodes[i]._element):
            return True
    return False


def extract_hierarchical_sections(nodes, description, base_items) -> list:

    outline_tree = build_outline_tree(nodes, description, base_items)
    mode = outline_tree.get("summary", {}).get("mode")
    outline = outline_tree.get("outline", [])
    if not outline or mode == "virtual_blocks":
        return extract_sequential_sections(nodes, description, base_items)

    major_sections = []
    for entry in outline:
        ir = entry.get("index_range", [0, 0])
        major_sections.append({
            "title": entry.get("title", "未命名章节"),
            "level": entry.get("level", 1),
            "start": ir[0],
            "end": ir[1],
            "subsections": entry.get("children", []),
        })
    return major_sections


def run_global_rewrite(llm, dialog: str, retrieved_info: str, doc: Document, nodes, base_items, description) -> Tuple[int, str]:

    sections = extract_hierarchical_sections(nodes, description, base_items)
    if not sections:
        return 0, "无法划分文档章节结构进行全局重构"

    print(f"【全局重塑】共划分为 {len(sections)} 个大章节进行统筹重塑与润色：")
    for s in sections:
        subs = s.get("subsections", [])
        sub_desc = f"（含 {len(subs)} 个子章节）" if subs else ""
        print(f"   - {s['title']} {sub_desc} [元素下标 {s['start']}~{s['end']}]")

    # 先生成一份全局纲要，约束各章分别重塑时的语气、编号与术语口径保持一致
    global_brief = _build_global_brief(llm, dialog, sections)
    if global_brief:
        print(f"【全局重塑】全局改写纲要：{global_brief[:120]}")
    brief_line = f"全局改写纲要（各章必须共同遵守）：{global_brief}\n" if global_brief else ""

    # 倒序处理各章节，避免插入/删除导致后续未处理章节的节点引用或下标错位
    total_sections_rewritten = 0
    skipped_sections = []
    failed_sections = []
    for s in reversed(sections):
        start = s["start"]
        end = s["end"]
        if _section_is_risky(nodes, description, start, end):
            print(f"大章节 {s['title']} 含图片/文本框等无法用结构表达的内容，已保留原内容不做重塑")
            skipped_sections.append(s["title"])
            continue

        # 如果大章节包含子章节，构建子章节说明
        sub_list_str = ""
        if s.get("subsections"):
            sub_lines = [
                f"  - [{sub['index_range'][0]}~{sub['index_range'][1]}] {sub['title']}"
                for sub in s["subsections"]
            ]
            sub_list_str = "本大章节包含以下子章节，重写时请保持父子层级结构，层次分明：\n" + "\n".join(sub_lines) + "\n"

        chunk_ranges = _split_section_ranges(description, start, end)
        if len(chunk_ranges) > 1:
            print(f"大章节 {s['title']} 内容较长，已按元素边界拆成 {len(chunk_ranges)} 块分别重塑")
        chunk_failed = False
        for chunk_start, chunk_end in chunk_ranges:
            sec_desc = description[chunk_start:chunk_end + 1]
            print(f"正在重写大章节：{s['title']}（元素下标 {chunk_start}~{chunk_end}，共 {chunk_end - chunk_start + 1} 个元素）...")

            prompt = (
                "你是一个企业公文与专业文档全局重构润色助手。你正在对文档进行逐章深度润色与重写。\n"
                f"用户的总体要求：{dialog}\n"
                f"{retrieved_info}"
                f"{brief_line}"
                f"当前重构大章节：【{s['title']}】（元素下标 {chunk_start}~{chunk_end}）\n"
                f"{sub_list_str}"
                "该章节原内容结构如下（包含段落与表格）：\n"
                f"{json.dumps(sec_desc, ensure_ascii=False)}\n"
                "请根据总体要求，输出该大章节完整的新内容（DocxRoot 格式）。\n"
                "格式规范：\n"
                "1. 严格保持父子章节层级结构：大章节标题、子章节标题必须加粗（bold=True），正文不加粗；\n"
                "2. 章节编号规范统一（如大章节用“一、”，子章节用“（一）”，或采用“1.”“1.1”等专业规范）；\n"
                "3. 如有表格，按表格模型输出，并保持原有的合并单元格结构；\n"
                "4. 保持严谨公文用词，结构清晰，文笔流畅；\n"
                "5. 充分吸纳可供参考的素材与最新数据。"
            )
            res = llm_model_invoke(llm, prompt, DocxRoot)
            if res is None or not res.items:
                print(f"大章节 {s['title']}（下标 {chunk_start}~{chunk_end}）重写未能生成有效内容，保留原内容")
                chunk_failed = True
                continue

            # 过滤出真正可渲染的有效项，防止空载荷导致原内容被错误清空
            valid_items = [
                item for item in res.items
                if (item.type == "paragraph" and item.Paragraph) or (
                    item.type == "table" and item.Table and item.Table.rows > 0 and item.Table.cols > 0 and item.Table.grid
                )
            ]
            if not valid_items:
                print(f"大章节 {s['title']}（下标 {chunk_start}~{chunk_end}）重写未能生成可渲染的有效内容，保留原内容")
                chunk_failed = True
                continue

            target_element = nodes[chunk_start]._element
            # 将新生成的段落和表格依次插入到旧内容首个元素之前
            for item in valid_items:
                if item.type == "paragraph" and item.Paragraph:
                    new_node = doc.add_paragraph("")
                    replace_node_with_data(new_node, item.Paragraph, doc=doc, write_defaults=False)
                    target_element.addprevious(new_node._element)
                elif item.type == "table" and item.Table:
                    new_table = replace_node_with_data(
                        doc.add_table(rows=0, cols=0), item.Table, doc=doc, write_defaults=False
                    )
                    target_element.addprevious(new_table._element)

            # 移除该块所有旧元素
            for i in range(chunk_start, chunk_end + 1):
                old_elem = nodes[i]._element
                parent = old_elem.getparent()
                if parent is not None:
                    parent.remove(old_elem)

        if chunk_failed:
            failed_sections.append(s["title"])
        else:
            total_sections_rewritten += 1

    if skipped_sections:
        print(f"【全局重塑】以下 {len(skipped_sections)} 个大章节含图片等无法改写的内容，已原样保留："
              + "、".join(skipped_sections))
    if failed_sections:
        print(f"【全局重塑】以下 {len(failed_sections)} 个大章节未能生成有效内容，已原样保留："
              + "、".join(failed_sections))
    if total_sections_rewritten == 0:
        return 0, "全局重塑未能改写任何大章节（所有章节都含图片等无法表达的内容，或生成失败）"
    return total_sections_rewritten, ""


def create_rewrite_node(llm):
    """创建改写节点，根据 state['rewrite_mode'] 分流调度三层改写引擎。"""
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
        print(f"待改写文档共 {len(nodes)} 个元素")

        retrieved_items = state.get("retrieved_content") or []
        retrieved_info = ""
        if retrieved_items:
            retrieved_info = "可供参考的信息：\n" + "\n".join(retrieved_items) + "\n"

        dialog = f"{state['messages']}"
        mode = (state.get("rewrite_mode") or "patch").lower()

        # 三层模式分流调度
        if mode == "replace":
            print("【改写调度】执行模式二：全局查找替换")
            applied, error = run_replace_rewrite(llm, dialog, doc, state.get("replace_pairs"))
        elif mode == "global":
            print("【改写调度】执行模式三：全局重构与全文润色")
            applied, error = run_global_rewrite(llm, dialog, retrieved_info, doc, nodes, base_items, description)
        else:
            print("【改写调度】执行模式一：局部微调修补")
            applied, error = run_patch_rewrite(llm, dialog, retrieved_info, doc, nodes, base_items, description)

        if error:
            return {**state, "state": "error", "error": error}

        if applied == 0:
            print("未产生任何有效修改，文档内容保持不变")

        if state.get("save_path"):
            save_path = resolve_save_path(state["save_path"])
        else:
            save_path = Path(rewrite_path)

        save_document(doc, save_path)
        print("改写后的文档已保存到：", str(save_path))
        return {**state, "state": "succeeded", "save_path": str(save_path)}

    return rewrite_node
