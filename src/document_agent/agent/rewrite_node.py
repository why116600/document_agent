import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run
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
    apply_paragraph_item_format,
    apply_run_item_format,
    apply_heading_outline_level,
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

MAX_DIRECT_TOKENS = 6000 #估算token低于该值的文档直接整篇改写，超长文档才工具化按需改写
MAX_TOOL_ROUNDS = 20 #工具化改写的最大轮数
FEEDBACK_MAX_ROUNDS = 4 #放进prompt的最近轮数
FEEDBACK_MAX_CHARS_PER_ROUND = 600 #单轮工具结果最多保留的字符数
MAX_SECTION_TOKENS = 4000 #全局重塑时单次送入模型的章节最大token
SEARCH_MAX_HITS = 20 #search工具最多返回的命中元素数
SEARCH_MAX_TERMS = 5 #search工具最多拆分的组合关键词数
SEARCH_MAX_HITS_PER_ELEMENT = 3 #单个元素内最多返回的命中位置数
MAX_READ_CHARS = 1500 #read工具单次最多返回的字符数
SEARCH_CONTEXT_BEFORE = 50 #search结果中关键词前保留的字符数
SEARCH_CONTEXT_AFTER = 60 #search结果中关键词后保留的字符数
HEADING_MAX_CHARS = 35 #启发式判定标题时的最大字数
HEADING_MIN_FONT_SIZE = 14 #启发式判定标题时的大字号阈值
VIRTUAL_BLOCK_SIZE = 18 #无标题文档按多少个元素聚合成一个虚拟块
FORMAT_HEADING_SIZE_STEP = 3 #格式规范化时标题字号相对正文基准的递增磅值
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

    # 1. 科技/标准规范体例 (GB/T 1.1)：存在 1.1 / 1.1.1
    if has_tech_dotted:
        return _build_patterns({
            1: [_P_CHAPTER, _P_ARABIC_1],
            2: [_P_SECTION, [_P_ARABIC_2, _P_CN_NUM]],
            3: [[_P_ARABIC_3], _P_CN_PAREN],
            4: [[_P_ARABIC_4], _P_ARABIC_PAREN],
        })

    # 2. 篇章规章体例（第一章/第一节等大章节）
    if has_chapter:
        return _build_patterns({
            1: [_P_CHAPTER],
            2: [_P_SECTION, [_P_CN_NUM, _P_ARABIC_2]],
            3: [_P_CN_PAREN, [_P_ARABIC_3, _P_ARABIC_1[0], _P_ARABIC_1[1]]],
            4: [_P_ARABIC_PAREN, [_P_ARABIC_4]],
        })

    # 3. 党政机关公文体例 (GB/T 9704-2012)：一级“一、”，二级“（一）”，三级“1.”，四级“（1）”
    if has_cn_num:
        return _build_patterns({
            1: [[_P_CN_NUM], _P_CHAPTER],
            2: [_P_CN_PAREN, _P_SECTION],
            3: [[_P_ARABIC_1[0], _P_ARABIC_1[1], _P_ARABIC_2]],
            4: [_P_ARABIC_PAREN, [re.compile(r"^\d+\.\d+\.\d+(\.(?!\d)|\s+|[、.．]|\b)")]],
        })

    # 4. 默认通用体例
    return DEFAULT_HEADING_PATTERNS


def describe_heading_style(nodes_or_texts=None) -> Optional[str]:
    """识别文档现有编号体例，返回自然语言描述；None 表示无可识别体系（由模型自选）。"""
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

    if has_tech_dotted:
        return "科技/标准规范体例（如“1.”“1.1”“1.1.1”）"
    if has_chapter:
        return "篇章规章体例（如“第一章”“第一节”，下设中文序号）"
    if has_cn_num:
        return "党政机关公文体例（“一、”为一级，“（一）”为二级，“1.”为三级，“（1）”为四级）"
    return None


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
    #取出元素的纯文本
    if item.get("type") == "paragraph":
        return "".join(run.get("text", "") for run in item.get("runs", []))
    if item.get("type") == "table":
        return " ".join(str(cell) for row in item.get("cells", []) for cell in row)
    return ""


def _get_outline_level(node):
    #读取段落大纲级别（w:outlineLvl），未设置返回None
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
    #正文里"加粗或大字号"的短段落，作为无编号标题的线索
    if not isinstance(base_item, ParagraphItem) or not base_item.runs:
        return False
    first_run = base_item.runs[0]
    if first_run.bold:
        return True
    return bool(first_run.font_size and first_run.font_size >= HEADING_MIN_FONT_SIZE)


def _detect_heading(node, base_item, text, patterns=None):
    """识别标题，返回(层级, 标题文本)；依次尝试：标准标题样式 → 大纲级别 → 编号正则 → 短文本且加粗/大字号。"""
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


#识别落款/结尾元数据的正则与关键词（可随知识库补充更新）
TAIL_DATE_PATTERN = re.compile(r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-./]\d{1,2}[-./]\d{1,2})")
TAIL_ORG_SUFFIXES = ("办公室", "部", "委员会", "局", "处", "科", "组", "公室", "公司", "院", "中心", "学会", "协会")
TAIL_SPECIAL_KEYWORDS = ("抄报", "抄送", "印发", "签发", "分发", "记录人", "记录：", "主持人", "主持：", "出席：", "列席：", "特此纪要", "特此通知", "特此报告")


def _detect_paragraph_role(node, base_item, text: str, index: int, total_elements: int, patterns=None) -> str:
    if not isinstance(node, Paragraph):
        return "table" if isinstance(node, Table) else "unknown"
    if not text:
        return "body"

    # 1. 优先识别标题
    if _detect_heading(node, base_item, text, patterns=patterns) is not None:
        return "heading"

    # 2. 文档末尾 6 个元素内的落款/日期
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
            # 过滤标题（加粗）、极短文本与居右落款
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
    #截取关键词命中位置的前后文
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
    #校验区间连续覆盖0~total-1且不重叠，防止后续读写错位
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
    #取区间内第一个有正文的元素作为章节预览
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
    #无标题文档兜底：按固定元素数聚合成虚拟块
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
        # 1. 层级归一化：顶层不是“第1章”时整体平移，使最高标题为 Level 1
        min_level = min(s["level"] for s in raw_sections)
        if min_level > 1:
            for s in raw_sections:
                s["level"] = max(1, s["level"] - (min_level - 1))

        # 2. 首个标题之前的内容作为前言
        if raw_sections[0]["start"] > 0:
            raw_sections.insert(0, {"level": 1, "title": "（文档开头/前言）", "start": 0})

        # 3. 章节 end 取其后首个层级不高于它的标题的 start - 1（无则到文档末尾）
        for i, s in enumerate(raw_sections):
            end = total - 1
            for j in range(i + 1, len(raw_sections)):
                if raw_sections[j]["level"] <= s["level"]:
                    end = raw_sections[j]["start"] - 1
                    break
            s["end"] = end

        # 4. 构建层级树
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
    return _format_outline_text(outline_tree)


def _split_terms(query: str) -> list:
    #按分隔符拆成多个关键词，任一命中即返回
    parts = re.split(r"[\s,，、;；|/]+", query)
    return [p.strip() for p in parts if p.strip()][:SEARCH_MAX_TERMS]


def _search_in_table(item, terms) -> Optional[dict]:
    #表格命中：返回表头与命中的单元格
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
    #段落命中：报告全部命中位置
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
    #按关键词搜索，返回命中元素下标与前后文
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
    #读取指定范围的完整内容，单次有上限
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
    #校验并记录一条或多条修改
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
    #执行工具，返回给模型的反馈文本
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
    #单轮工具结果超长时截断
    if len(text) <= limit:
        return text
    return f"{text[:limit]}……(已截断，原文共{len(text)}字，如需完整内容请read更小的范围)"


def _rewrite_with_tools(llm, dialog, nodes, description, base_items):
    #工具化改写：模型按需探查文档并逐条提交修改，返回(修改列表, 错误信息)
    outline_tree = build_outline_tree(nodes, description, base_items)
    summary = outline_tree.get("summary", {})
    print(f"文档大纲模式：{summary.get('mode')}，元素{summary.get('elements')}个，约{summary.get('chars')}字")
    for entry in outline_tree.get("outline", [])[:10]:
        print("   ", entry.get("title") or entry.get("head") or "", entry.get("index_range"))
    edited = []
    history = []
    for _ in range(MAX_TOOL_ROUNDS):
        #只把最近几轮的工具结果放进prompt
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
                    continue  #横向合并的同一 tc 会被重复返回
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
                    #统计下方同列 vMerge=continue 的行数作为纵向跨度
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
    #解析文档元素，供改写时准确定位
    nodes = list(doc.iter_inner_content())
    total = len(nodes)
    patterns = get_heading_patterns_for_doc(nodes)
    base_items = []
    description = []
    section_stack = []  # 当前标题层级栈 [(level, title)]

    for index, node in enumerate(nodes):
        if isinstance(node, Paragraph):
            try:
                #preserve_none=True：未显式设置的格式保留为None，以区分"原文没设置"与"显式默认值"
                item = convert_node(node, preserve_none=True)
            except Exception as e:
                print(f"解析第{index}个段落失败，按纯文本处理：{e}")
                item = ParagraphItem(runs=[TextRunItem(text=node.text, text_type="text")])
            base_items.append(item)
            para_dict = _paragraph_to_dict(item)
            if paragraph_has_protected_content(node):
                #标记含图片等无法表达的内容，改写时保留元素、只替换文字
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
                #告知模型合并单元格的位置与跨度，重写表格时保持合并结构
                table_dict["merges"] = merges
            description.append(table_dict)
        else:
            base_items.append(None)
            unk_dict = {"index": index, "type": "unknown", "role": "unknown"}
            if section_stack:
                unk_dict["section_path"] = "/".join(s[1] for s in section_stack)
            description.append(unk_dict)
    return nodes, base_items, description #返回对象、格式模型、结构描述

def _inherit_run_format(new_run: TextRunItem, base_run: Optional[TextRunItem]) -> TextRunItem:
    #模型显式给出的格式优先，未给出（None）的字段才沿用原文格式
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


def apply_rewrite_plan(doc: Document, nodes, base_items, edits, description=None) -> Tuple[int, List[str]]:
    #按下标从大到小执行，避免增删导致后续下标错位；返回(生效数, 未落地的修改明细)
    applied = 0
    failed: List[str] = []
    body_profile = extract_body_profile(base_items)

    #扫描文档末尾连续的 tail_meta（落款/日期等），定位落款起始下标
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

    #同一下标可同时替换和追加，故按动作优先级排序；下标从大到小处理避免错位
    action_priority = {"replace": 0, "replace_text": 0, "insert_after": 1, "delete": 2}
    ordered_edits = sorted(
        edits or [],
        key=lambda item: (-item.index, action_priority.get((item.action or "").strip().lower(), 9)),
    )
    #同一下标同一动作只保留最后一次提交，给模型自我纠错的机会
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
        #在文档末尾追加时，锚点回退到最后一个元素
        if action == "insert_after" and edit.index >= len(nodes) and len(nodes) > 0:
            edit.index = len(nodes) - 1

        #在落款及其之后做 insert_after 时自动前移至正文末尾，保持公文版式规范
        if action == "insert_after" and edit.index >= tail_start_index and tail_start_index > 0:
            target_index = tail_start_index - 1
            print(f"[智能纠偏] 落款/日期后的插入已前移至正文末尾（第{target_index}个元素后）")
            edit.index = target_index

        if edit.index < 0 or edit.index >= len(nodes):
            print(f"忽略越界的修改下标：{edit.index}")
            failed.append(f"第{edit.index}个元素的{action}未生效：下标越界（文档共{len(nodes)}个元素）")
            continue
        node = nodes[edit.index]
        base_item = base_items[edit.index]
        try:
            if action == "delete":
                if element_has_protected_content(node._element):
                    print(f"第{edit.index}个元素含图片等无法表达的内容，已跳过删除")
                    continue
                node._element.getparent().remove(node._element)
                applied += 1
                print(f"已删除第{edit.index}个元素")
            elif action == "replace":
                if isinstance(node, Table):
                    if not isinstance(edit.table, TableItem) or edit.table.rows <= 0 or edit.table.cols <= 0 or not edit.table.grid:
                        print(f"第{edit.index}个元素是表格，但未提供有效的表格内容，跳过替换")
                        failed.append(f"第{edit.index}个表格未改写：模型未给出有效的表格内容")
                        continue
                    #表格替换会重建对象，必须写回nodes，否则同下标的insert_after会拿到已移除的旧元素
                    if element_has_protected_content(node._element):
                        print(f"第{edit.index}个表格含图片等无法表达的内容，已跳过替换")
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
                        failed.append(f"第{edit.index}个段落未改写：模型未给出改写后的段落内容")
                        continue
                    #首段原地替换并继承原段落格式（is_insert=False）
                    replace_node_with_data(node, _inherit_paragraph_format(replace_items[0], base_item, body_profile, is_insert=False), doc, write_defaults=False)
                    #其余段落依次插到其后，使用正文基准格式（is_insert=True）
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
                    failed.append(f"第{edit.index}个元素后未插入内容：模型给出的插入内容为空")
                    continue
                #addnext 逐个插到锚点后，故倒序生成才能保持模型给出的顺序
                for item in reversed(insert_items):
                    new_paragraph = doc.add_paragraph("")
                    replace_node_with_data(new_paragraph, _inherit_paragraph_format(item, base_item, body_profile, is_insert=True), write_defaults=False)
                    #add_paragraph 会追加到文档末尾，这里移到目标元素之后
                    node._element.addnext(new_paragraph._element)
                applied += len(insert_items)
                print(f"已在第{edit.index}个元素后插入{len(insert_items)}个新段落")
            elif action == "replace_text":
                #精确子串替换：保留模型给出的原文首尾空格，避免匹配错位（纯空白已在校验层拒绝）
                old_text = edit.old_text or ""
                if not old_text:
                    print(f"第{edit.index}个元素缺少old_text，跳过")
                    failed.append(f"第{edit.index}个元素未做子串替换：模型未给出 old_text")
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
                failed.append(f"第{edit.index}个元素未处理：未知的修改动作“{edit.action}”")
        except Exception as e:
            print(f"应用第{edit.index}个元素的修改失败：{e}")
            failed.append(f"第{edit.index}个元素的{action}执行异常：{e}")
    return applied, failed

class ReplacementMapping(BaseModel):
    pairs: Dict[str, str] = Field(
        default_factory=dict,
        description="需要查找替换的词语对映射，key为原词/旧词，value为替换后的新词"
    )


def run_patch_rewrite(llm, dialog: str, retrieved_info: str, doc: Document, nodes, base_items, description) -> Tuple[int, str]:
    estimated_tokens = _estimate_description_tokens(description)
    if estimated_tokens <= MAX_DIRECT_TOKENS:
        print(f"【局部修补】文档约{estimated_tokens} tokens，直接整篇改写")
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
        print(f"【局部修补】文档约{estimated_tokens} tokens，启用工具化改写")
        edits, error = _rewrite_with_tools(llm, f"{dialog}\n{retrieved_info}", nodes, description, base_items)
        if error:
            if not edits:
                return 0, error
            #轮次跑满但已有暂存的修改：部分落地好过整体丢弃
            print(f"【局部修补】警告：{error}；仍将落地已暂存的 {len(edits)} 条修改，请核对是否覆盖全部要求")
            error = ""
        if not edits:
            return 0, "模型没有提交任何修改"

    applied, failed = apply_rewrite_plan(doc, nodes, base_items, edits, description)
    if failed:
        print(f"【局部修补】有 {len(failed)} 条修改未能生效：")
        for detail in failed:
            print(f"   - {detail}")
        if applied == 0:
            return 0, "全部修改均未能生效：" + "；".join(failed)
        #部分失败也要回报明细，不能静默当成全部成功
        return applied, f"{len(failed)} 条修改未能生效（另 {applied} 条已写入）：" + "；".join(failed)
    return applied, ""


def run_replace_rewrite(llm, dialog: str, doc: Document, replace_pairs: Optional[Dict[str, str]] = None) -> Tuple[int, str]:

    pairs = dict(replace_pairs or {})
    if not pairs:
        print("【全局替换】未传入替换对，尝试从需求中提取")
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
            print(f"【全局替换】提取到替换对：{pairs}")
        else:
            return 0, "未能从用户需求中识别出需要替换的目标词和新词，请明确说明或使用 --find 与 --replace-with 参数"

    if not pairs:
        return 0, "查找替换对为空"

    stats = batch_replace_in_document(doc, pairs)
    total_replaced = sum(stats.values())
    summary_parts = [f"“{k}” -> “{v}” ({stats.get(k, 0)}处)" for k, v in pairs.items()]
    print(f"【全局替换】共替换 {total_replaced} 处：" + "，".join(summary_parts))
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
        #只识别出一个标题时也作为语义边界，避免标题被夹在虚拟块中间
        sections = []
        if headings[0][0] > 0:
            sections.append({"title": "前言/引言", "start": 0, "end": headings[0][0] - 1})
        sections.append({"title": headings[0][2], "start": headings[0][0], "end": total - 1})
        if _ranges_valid([(s["start"], s["end"]) for s in sections], total):
            return sections

    # 兜底：按虚拟块切分
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


def _build_global_brief(llm, dialog: str, sections, style_hint: Optional[str] = None) -> str:

    try:
        outline_text = json.dumps(
            [{"title": s.get("title"), "index_range": [s.get("start"), s.get("end")]} for s in sections],
            ensure_ascii=False,
        )
        style_line = (
            f"原文档现有编号体例：{style_hint}。默认沿用该体例，但若用户要求中明确指定了其他体例则以用户要求为准。\n"
            if style_hint
            else "原文档无明确编号体系，请根据文档类型与内容风格，自行选择一套最合适的规范编号体例并写入纲要。\n"
        )
        prompt = (
            "你是企业公文重构的统筹助手。下面是文档大纲与用户的总体要求，"
            "请给出一份简短的全局改写纲要，用于约束各章节分别改写时的统一性。\n"
            f"用户总体要求：{dialog}\n"
            f"文档大纲：{outline_text}\n"
            f"{style_line}"
            "请输出不超过300字的纲要，包含：全文语气风格、章节编号与称谓口径、关键术语的统一写法、"
            "各章需要保持一致的数据口径。仅输出纲要本身。\n"
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

    print(f"【全局重塑】共划分为 {len(sections)} 个大章节：")
    for s in sections:
        subs = s.get("subsections", [])
        sub_desc = f"（含 {len(subs)} 个子章节）" if subs else ""
        print(f"   - {s['title']} {sub_desc} [元素下标 {s['start']}~{s['end']}]")

    #识别原文档编号体例并回传给生成端：有则默认沿用，无则由模型自由选配
    style_hint = describe_heading_style(nodes)
    if style_hint:
        numbering_directive = (
            f"原文档采用{style_hint}，默认应沿用该编号体例；"
            "但若用户在改写要求中明确指定了其他编号体例，或原文体例本身混乱不规范需要统一，"
            "则以用户要求或规范化目标为准"
        )
        print(f"【全局重塑】识别到编号体例：{style_hint}，默认沿用")
    else:
        numbering_directive = (
            "原文档无明确编号体系，请根据文档类型、内容风格与行业惯例，"
            "自行选择一套最合适的规范编号体例（不限于任何特定体例），"
            "并确保全文层级一致、体例统一"
        )
        print("【全局重塑】原文档无明确编号体系，由模型自行选择体例")

    #先生成全局纲要，约束各章的语气、编号与术语口径一致
    global_brief = _build_global_brief(llm, dialog, sections, style_hint)
    if global_brief:
        print(f"【全局重塑】全局改写纲要：{global_brief[:120]}")
    brief_line = f"全局改写纲要（各章必须共同遵守）：{global_brief}\n" if global_brief else ""

    #倒序处理各章节，避免增删导致后续章节下标错位
    total_sections_rewritten = 0
    skipped_sections = []
    failed_sections = []
    for s in reversed(sections):
        start = s["start"]
        end = s["end"]
        if _section_is_risky(nodes, description, start, end):
            print(f"大章节 {s['title']} 含图片/文本框等无法表达的内容，保留原内容")
            skipped_sections.append(s["title"])
            continue

        #构建子章节说明
        sub_list_str = ""
        if s.get("subsections"):
            sub_lines = [
                f"  - [{sub['index_range'][0]}~{sub['index_range'][1]}] {sub['title']}"
                for sub in s["subsections"]
            ]
            sub_list_str = "本大章节包含以下子章节，重写时请保持父子层级结构，层次分明：\n" + "\n".join(sub_lines) + "\n"

        chunk_ranges = _split_section_ranges(description, start, end)
        if len(chunk_ranges) > 1:
            print(f"大章节 {s['title']} 内容较长，已拆成 {len(chunk_ranges)} 块分别重塑")
        chunk_failed = False
        staged_chunks = []  #暂存各块生成结果，待本章节所有块都成功后再统一落盘，避免中途失败留下半改
        for chunk_start, chunk_end in chunk_ranges:
            sec_desc = description[chunk_start:chunk_end + 1]
            print(f"正在重写大章节：{s['title']}（下标 {chunk_start}~{chunk_end}）...")

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
                "2. 标题段落必须设置 heading_level 字段写入真实大纲级别：大章节标题 heading_level=1，"
                "子章节标题 heading_level=2，更深层级依次为 3、4；正文段落不设置该字段；\n"
                f"3. 章节编号规范统一：{numbering_directive}；\n"
                "4. 字体字号处理原则（先判断用户意图，再决定是否设置 font_name/font_size 字段）：\n"
                "   - 若用户要求中【未】提及格式调整（只要求改内容、润色文字等）："
                "不要设置 font_name/font_size 等字体字段，所有段落沿用原文档格式；\n"
                "   - 若用户明确要求规范/修改格式（如“格式规范一下”“统一排版”“调整字体”等）："
                "请你根据该文档的类型、用途与行业惯例，自行思考并设计一套最适合本文档的篇章格式方案"
                "（主标题、各级标题、正文各自的字体与字号梯度），要求同层级完全一致、全文统一不混用、"
                "标题与正文的层级梯度清晰，并通过 font_name/font_size 字段落实到每个段落；\n"
                "5. 如有表格，按表格模型输出，并保持原有的合并单元格结构；\n"
                "6. 保持严谨公文用词，结构清晰，文笔流畅；\n"
                "7. 充分吸纳可供参考的素材与最新数据。"
            )
            res = llm_model_invoke(llm, prompt, DocxRoot)
            if res is None or not res.items:
                print(f"大章节 {s['title']}（下标 {chunk_start}~{chunk_end}）重写未能生成有效内容，保留原内容")
                chunk_failed = True
                break

            #过滤出可渲染的有效项，防止空载荷把原内容清空
            valid_items = [
                item for item in res.items
                if (item.type == "paragraph" and item.Paragraph) or (
                    item.type == "table" and item.Table and item.Table.rows > 0 and item.Table.cols > 0 and item.Table.grid
                )
            ]
            if not valid_items:
                print(f"大章节 {s['title']}（下标 {chunk_start}~{chunk_end}）未生成可渲染内容，保留原内容")
                chunk_failed = True
                break

            staged_chunks.append((chunk_start, valid_items))

        if chunk_failed:
            failed_sections.append(s["title"])
            continue

        #本章节所有块都生成成功，才统一落盘：先插入全部新内容，再删除各块旧元素
        for chunk_start, valid_items in staged_chunks:
            target_element = nodes[chunk_start]._element
            #新内容依次插入到旧内容首个元素之前
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

        # 移除各块所有旧元素
        for chunk_start, chunk_end in chunk_ranges:
            for i in range(chunk_start, chunk_end + 1):
                old_elem = nodes[i]._element
                parent = old_elem.getparent()
                if parent is not None:
                    parent.remove(old_elem)

        total_sections_rewritten += 1

    if skipped_sections:
        print(f"【全局重塑】以下 {len(skipped_sections)} 个大章节含无法改写的内容，已原样保留："
              + "、".join(skipped_sections))
    if failed_sections:
        print(f"【全局重塑】以下 {len(failed_sections)} 个大章节生成失败，已原样保留："
              + "、".join(failed_sections))
    if total_sections_rewritten == 0:
        return 0, "全局重塑未能改写任何大章节（所有章节都含图片等无法表达的内容，或生成失败）"
    return total_sections_rewritten, ""


# ================== 模式四：格式规范化（只改排版，不动文字） ==================

class RoleFormatSpec(BaseModel):
    """单一角色（标题/正文/落款/表格文字）的排版格式规范。"""

    font_name: Optional[str] = Field(default=None, description="字体名，如宋体、黑体、楷体、微软雅黑")
    font_size: Optional[float] = Field(default=None, description="字号磅值，如12（小四）、14（四号）、16（三号）")
    bold: Optional[bool] = Field(default=None, description="是否加粗")
    color: Optional[str] = Field(default=None, description="字体颜色，形如 #000000")
    alignment: Optional[str] = Field(default=None, description="对齐方式：left/center/right/justify")
    line_spacing: Optional[float] = Field(default=None, description="行距倍数，如1.5")
    spacing_before: Optional[float] = Field(default=None, description="段前间距（磅）")
    spacing_after: Optional[float] = Field(default=None, description="段后间距（磅）")
    first_line_indent: Optional[float] = Field(default=None, description="首行缩进（磅），留空则保持原样")
    left_indent: Optional[float] = Field(default=None, description="左缩进（磅），留空则保持原样")
    right_indent: Optional[float] = Field(default=None, description="右缩进（磅），留空则保持原样")


class DocumentFormatSpec(BaseModel):
    """整篇文档的格式规范：只描述排版样式，不涉及文字内容。"""

    summary: str = Field(default="", description="格式方案的简要说明")
    heading1: Optional[RoleFormatSpec] = Field(default=None, description="一级标题格式")
    heading2: Optional[RoleFormatSpec] = Field(default=None, description="二级标题格式")
    heading3: Optional[RoleFormatSpec] = Field(default=None, description="三级标题格式")
    heading4: Optional[RoleFormatSpec] = Field(default=None, description="四级标题格式")
    body: Optional[RoleFormatSpec] = Field(default=None, description="正文段落格式")
    tail_meta: Optional[RoleFormatSpec] = Field(default=None, description="落款/日期等文末元信息格式")
    table_text: Optional[RoleFormatSpec] = Field(default=None, description="表格内文字格式")
    apply_outline_level: bool = Field(
        default=True, description="是否把识别到的标题写入 Word 大纲级别（便于导航窗格与自动目录）"
    )


FORMAT_PREVIEW_CHARS = 60  #体裁方案里每段预览的字符数


class ParagraphRoleAssignment(BaseModel):
    """逐段角色分配：index 为文档元素下标，role 为体裁相关的开放角色名。"""

    index: int = Field(description="元素下标（从0开始，须与段落预览里的 index 一致）")
    role: str = Field(description="该段落归属的角色名（开放词汇，如 email_header/email_body/separator/list_item/title/body 等）")


class RoleFormatEntry(BaseModel):
    """单个角色及其排版格式规范。"""

    role: str = Field(description="角色名，须与 paragraph_roles 中出现的角色名一致")
    format: RoleFormatSpec = Field(default_factory=RoleFormatSpec, description="该角色的排版格式规范，留空字段表示保持原样")


class GenreFormatPlan(BaseModel):
    """体裁感知的排版方案：先判断体裁，再逐段分配角色，并为每个角色定义格式。"""

    genre: str = Field(default="", description="文档体裁判断，如 邮件往来/会议纪要/公文/报告/合同/清单")
    paragraph_roles: List[ParagraphRoleAssignment] = Field(default_factory=list, description="每个段落元素对应的角色")
    role_formats: List[RoleFormatEntry] = Field(default_factory=list, description="每个实际用到的角色对应的排版格式规范")


def _paragraph_format_hint(item: dict, base_item) -> str:
    #概括段落当前格式，供模型判断角色
    if not isinstance(base_item, ParagraphItem) or not base_item.runs:
        return ""
    first_run = base_item.runs[0]
    parts = []
    if first_run.font_name:
        parts.append(first_run.font_name)
    if first_run.font_size:
        parts.append(f"{first_run.font_size:g}磅")
    if first_run.bold:
        parts.append("加粗")
    if first_run.font_color and str(first_run.font_color).startswith("#"):
        parts.append(first_run.font_color)
    if base_item.alignment and base_item.alignment != "left":
        parts.append(f"对齐={base_item.alignment}")
    return "，".join(parts)


def _format_preview(description, base_items) -> str:
    #拼装"index + 预览 + 当前格式"清单，供模型判断体裁与角色
    lines = []
    for i, meta in enumerate(description):
        if meta.get("type") != "paragraph":
            continue
        text = _item_text(meta).strip()
        preview = text[:FORMAT_PREVIEW_CHARS] + ("..." if len(text) > FORMAT_PREVIEW_CHARS else "")
        hint = _paragraph_format_hint(meta, base_items[i] if i < len(base_items) else None)
        line = f"[{meta.get('index')}] {preview}"
        if hint:
            line += f"  {{当前格式: {hint}}}"
        lines.append(line)
    return "\n".join(lines)


_HEADING_ROLE_HINTS = ("heading", "title", "标题")


def _role_is_heading(role: str) -> bool:
    #角色名是开放的，这里判断是否属标题/强调类（决定是否写大纲级别）
    r = (role or "").lower()
    return any(h in r for h in _HEADING_ROLE_HINTS)


def _propose_genre_format_plan(llm, dialog: str, retrieved_info: str, preview: str) -> Optional[GenreFormatPlan]:
    #让模型先判断体裁、逐段分配角色并设计格式（只输出排版，不产出文字）
    prompt = (
        "你是企业文档排版规范助手。请先判断文档体裁，再为每个段落分配角色，并为每个角色设计排版格式。\n"
        "【铁律】你只输出排版格式（字体、字号、加粗、颜色、对齐、行距、段距、缩进），"
        "绝对不要输出、改写、复述或建议任何正文文字内容。\n"
        f"用户要求：{dialog}\n"
        f"{retrieved_info}"
        f"文档各段落（index + 文本预览 + 当前格式，文本预览仅供判断角色，不得复述或改写）：\n{preview}\n"
        "设计要求：\n"
        "1. genre 判断文档体裁（如 邮件往来/会议纪要/公文/报告/合同/清单 等），据此确定角色体系；\n"
        "2. paragraph_roles 为每个段落分配一个角色名，角色名为开放词汇（如 email_header/email_body/separator/list_item/title/body/落款 等），"
        "体裁不同角色名可以不同，无需受固定列表约束；\n"
        "3. role_formats 为 paragraph_roles 中实际用到的每个角色各给一条格式，只填需要统一的字段，其余字段留空（保持原样）；\n"
        "4. 同一角色全文取值必须完全一致；标题/强调类角色字号要形成清晰梯度；\n"
        "5. 中文文档优先使用系统常见字体（宋体、黑体、楷体、仿宋、微软雅黑）；\n"
        "6. 字号单位为磅：小四=12、四号=14、小三=15、三号=16、小二=18、二号=22；\n"
        "7. 邮件头/分隔线/清单项等非成段正文不要设置首行缩进；正文是否首行缩进按体裁习惯决定。"
    )
    return llm_model_invoke(llm, prompt, GenreFormatPlan)


def _majority(values: list) -> Optional[Any]:
    #取多数取值；空列表返回 None，表示无依据、该字段保持原样
    return max(set(values), key=values.count) if values else None


def _collect_heading_level_stats(base_items, description) -> Dict[int, dict]:
    #统计原文各层级标题的实际排版（多数取值），用于同层级统一
    buckets: Dict[int, dict] = {}
    for index, meta in enumerate(description or []):
        if meta.get("type") != "paragraph":
            continue
        level = meta.get("heading_level")
        if not isinstance(level, int) or not (1 <= level <= 4):
            continue
        if index >= len(base_items):
            continue
        item = base_items[index]
        if not isinstance(item, ParagraphItem) or not item.runs:
            continue
        first_run = item.runs[0]
        bucket = buckets.setdefault(
            level, {"font_name": [], "font_size": [], "font_color": [], "alignment": [], "bold": []}
        )
        if first_run.font_name:
            bucket["font_name"].append(first_run.font_name)
        if first_run.font_size and 8 <= first_run.font_size <= 32:
            bucket["font_size"].append(first_run.font_size)
        if first_run.font_color and str(first_run.font_color).startswith("#"):
            bucket["font_color"].append(first_run.font_color)
        if item.alignment:
            bucket["alignment"].append(item.alignment)
        bucket["bold"].append(bool(first_run.bold))

    stats: Dict[int, dict] = {}
    for level, bucket in buckets.items():
        stats[level] = {
            "font_name": _majority(bucket["font_name"]),
            "font_size": _majority(bucket["font_size"]),
            "font_color": _majority(bucket["font_color"]),
            "alignment": _majority(bucket["alignment"]),
            "bold": True if _majority(bucket["bold"]) else None,
        }
    return stats


def _build_builtin_format_spec(base_items, description) -> DocumentFormatSpec:
    """内置方案：沿用文档自身设计，只做同角色统一与层级梯度校正。"""
    profile = extract_body_profile(base_items)
    body_size = profile.get("font_size") or DEFAULT_FONT_SIZE
    body_spec = RoleFormatSpec(
        font_name=profile.get("font_name"),
        font_size=body_size,
        color=profile.get("font_color"),
        alignment=profile.get("alignment"),
        line_spacing=profile.get("line_spacing"),
        spacing_before=profile.get("spacing_before"),
        spacing_after=profile.get("spacing_after"),
    )

    stats = _collect_heading_level_stats(base_items, description)
    present_levels = sorted(stats)
    max_level = present_levels[-1] if present_levels else 0

    sizes: Dict[int, float] = {}
    for level in present_levels:
        raw = stats[level].get("font_size")
        if raw is None:
            #原文未设置字号时，用正文基准（或标题最小字号）+ 递增步长补齐
            sizes[level] = max(body_size, HEADING_MIN_FONT_SIZE) + FORMAT_HEADING_SIZE_STEP * (max_level - level + 1)
        else:
            #标题不得小于正文，避免规范后层级反而弱于正文
            sizes[level] = max(raw, body_size)
    for upper, lower in zip(present_levels, present_levels[1:]):
        #层级字号单调不增：下级标题不得大于上级标题
        sizes[lower] = min(sizes[lower], sizes[upper])

    heading_specs: Dict[int, Optional[RoleFormatSpec]] = {1: None, 2: None, 3: None, 4: None}
    for level in present_levels:
        s = stats[level]
        heading_specs[level] = RoleFormatSpec(
            font_name=s.get("font_name") or profile.get("font_name"),
            font_size=sizes[level],
            #原文多数标题加粗才加粗，不做无依据的改动
            bold=s.get("bold"),
            color=s.get("font_color") or profile.get("font_color"),
            alignment=s.get("alignment"),
            line_spacing=profile.get("line_spacing"),
            spacing_before=profile.get("spacing_before"),
            spacing_after=profile.get("spacing_after"),
        )

    return DocumentFormatSpec(
        summary="内置统一排版方案：以原文正文格式为基准，同角色全文完全一致，标题字号形成清晰梯度",
        heading1=heading_specs[1],
        heading2=heading_specs[2],
        heading3=heading_specs[3],
        heading4=heading_specs[4],
        body=body_spec,
        tail_meta=None,
        table_text=RoleFormatSpec(
            font_name=profile.get("font_name"),
            font_size=body_size,
            color=profile.get("font_color"),
            line_spacing=profile.get("line_spacing"),
        ),
        apply_outline_level=True,
    )


def _role_spec_to_para_item(spec: Optional[RoleFormatSpec]) -> ParagraphItem:
    #把段落属性转成 ParagraphItem（不含文字，只承载排版属性）
    spec = spec or RoleFormatSpec()
    return ParagraphItem(
        alignment=spec.alignment,
        spacing_before=spec.spacing_before,
        spacing_after=spec.spacing_after,
        line_spacing=spec.line_spacing,
        first_line_indent=spec.first_line_indent,
        left_indent=spec.left_indent,
        right_indent=spec.right_indent,
    )


def _role_spec_to_run_item(spec: Optional[RoleFormatSpec]) -> TextRunItem:
    #把文字属性转成 TextRunItem（text 为空，只承载字体格式）
    spec = spec or RoleFormatSpec()
    return TextRunItem(
        text="",
        text_type="text",
        bold=spec.bold,
        font_name=spec.font_name,
        font_size=spec.font_size,
        font_color=spec.color,
    )


def _iter_paragraph_runs(paragraph: Paragraph) -> list:
    #同时覆盖普通 Run 与超链接内的 Run，保证同段文字样式统一
    try:
        return [Run(r, paragraph) for r in paragraph._p.xpath("w:r | w:hyperlink/w:r")]
    except Exception:
        return list(paragraph.runs)


def _apply_role_format_to_paragraph(paragraph: Paragraph, run_spec: Optional[RoleFormatSpec],
                                    para_spec: Optional[RoleFormatSpec]) -> None:
    #只写 w:pPr 与 w:rPr，不动 w:t 文本节点：图片/公式/域代码/超链接天然完好
    try:
        if para_spec is not None:
            apply_paragraph_item_format(paragraph, _role_spec_to_para_item(para_spec), write_defaults=False)
        if run_spec is not None:
            run_item = _role_spec_to_run_item(run_spec)
            for run in _iter_paragraph_runs(paragraph):
                apply_run_item_format(run, run_item, write_defaults=False)
    except Exception as e:
        print(f"【格式规范】跳过无法格式化的段落：{e}")


def _apply_role_format_to_table(table: Table, run_spec: Optional[RoleFormatSpec],
                                para_spec: Optional[RoleFormatSpec]) -> None:
    #表格内同样只改字体属性；合并单元格会被重复枚举，按底层元素去重
    seen = set()
    for row in table.rows:
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:
                continue
            seen.add(key)
            for paragraph in cell.paragraphs:
                _apply_role_format_to_paragraph(paragraph, run_spec, para_spec)


def _heading_spec_for_level(spec: DocumentFormatSpec, level: int) -> Optional[RoleFormatSpec]:
    return {1: spec.heading1, 2: spec.heading2, 3: spec.heading3, 4: spec.heading4}.get(level)


def _describe_role_spec(role_spec: Optional[RoleFormatSpec]) -> str:
    #把格式方案转成一行便于核对的文字
    if role_spec is None:
        return "保持原样"
    parts = []
    if role_spec.font_name:
        parts.append(f"字体={role_spec.font_name}")
    if role_spec.font_size is not None:
        parts.append(f"字号={role_spec.font_size:g}磅")
    if role_spec.bold is not None:
        parts.append("加粗" if role_spec.bold else "不加粗")
    if role_spec.color:
        parts.append(f"颜色={role_spec.color}")
    if role_spec.alignment:
        parts.append(f"对齐={role_spec.alignment}")
    if role_spec.line_spacing is not None:
        parts.append(f"行距={role_spec.line_spacing}")
    if role_spec.first_line_indent is not None:
        parts.append(f"首行缩进={role_spec.first_line_indent:g}磅")
    return "，".join(parts) if parts else "保持原样"


def _clip_text(text: Optional[str], limit: int = 20) -> str:
    #把文本截断成适合打印的短片段
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit]


def _document_text_length(nodes) -> int:
    #统计文档纯文字长度，用于格式规范化后的保真校验
    total = 0
    for node in nodes:
        if isinstance(node, Paragraph):
            total += len(node.text or "")
        elif isinstance(node, Table):
            for row in node.rows:
                for cell in row.cells:
                    total += len(cell.text or "")
    return total


def _document_text_snapshot(nodes) -> List[Optional[str]]:
    """逐元素记录纯文字快照用于保真校验；非段落/表格返回None表示不参与。"""
    snapshot: List[Optional[str]] = []
    for node in nodes:
        if isinstance(node, Paragraph):
            snapshot.append(node.text or "")
        elif isinstance(node, Table):
            seen = set()
            parts = []
            for row in node.rows:
                for cell in row.cells:
                    #合并单元格会重复指向同一个 w:tc，去重避免同一段文字被比对两次
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc)
                    parts.append(cell.text or "")
            snapshot.append("\n".join(parts))
        else:
            snapshot.append(None)
    return snapshot


def run_format_rewrite(llm, dialog: str, retrieved_info: str, doc: Document,
                       nodes, base_items, description) -> Tuple[int, str]:
    """模式四：格式规范化 —— 只改排版样式，原文文字不变。

    修改只落在 w:pPr 与 w:rPr 上，故图片、公式、域代码、超链接与文字内容天然完好。
    排版方案优先由大模型按体裁逐段分配角色（GenreFormatPlan），失败时回退内置方案。
    """
    builtin_spec = _build_builtin_format_spec(base_items, description)
    genre_plan = None
    if llm is not None:
        try:
            genre_plan = _propose_genre_format_plan(
                llm, dialog, retrieved_info, _format_preview(description, base_items)
            )
        except Exception as e:
            print(f"【格式规范】体裁排版方案生成失败，改用内置方案：{e}")

    plan_by_index: Dict[int, str] = {}
    role_formats: Dict[str, RoleFormatSpec] = {}
    if genre_plan is not None and genre_plan.paragraph_roles:
        for assign in genre_plan.paragraph_roles:
            plan_by_index[assign.index] = assign.role
        for entry in genre_plan.role_formats:
            if entry.format is not None:
                role_formats[entry.role] = entry.format
    spec = builtin_spec

    if plan_by_index:
        role_count: Dict[str, int] = {}
        for role in plan_by_index.values():
            role_count[role] = role_count.get(role, 0) + 1
        print(f"【格式规范】采用体裁感知排版方案（体裁：{genre_plan.genre or '未指定'}）")
        print(f"【格式规范】段落角色分布：{role_count}")
        for role, role_spec in role_formats.items():
            print(f"   - {role}: {_describe_role_spec(role_spec)}")
    else:
        print(f"【格式规范】采用方案：{spec.summary or '内置统一排版方案'}")
        for label, role_spec in (
            ("一级标题", spec.heading1), ("二级标题", spec.heading2),
            ("三级标题", spec.heading3), ("四级标题", spec.heading4),
            ("正文", spec.body), ("落款/日期", spec.tail_meta), ("表格文字", spec.table_text),
        ):
            print(f"   - {label}: {_describe_role_spec(role_spec)}")
    if not _collect_heading_level_stats(base_items, description):
        print("【格式规范】未识别到标题层级，仅统一正文与表格排版；如需梳理标题请改用全局重塑模式")
    print("【格式规范】大纲级别写入：" + ("开启" if spec.apply_outline_level else "关闭"))

    before_len = _document_text_length(nodes)
    before_texts = _document_text_snapshot(nodes)
    heading_applied = 0
    body_applied = 0
    table_applied = 0
    skipped = 0

    for index, node in enumerate(nodes):
        meta = description[index] if index < len(description) else {}
        if isinstance(node, Paragraph):
            level = meta.get("heading_level")
            detected_role = meta.get("role") or "body"
            genre_role = plan_by_index.get(index)

            run_spec = para_spec = None
            is_heading = False
            if genre_role is not None and genre_role in role_formats:
                #体裁方案优先：模型为该段分配了角色并给出了格式
                gspec = role_formats[genre_role]
                run_spec = gspec
                para_spec = gspec
                is_heading = _role_is_heading(genre_role) or detected_role == "heading"
            elif detected_role == "heading" and isinstance(level, int) and 1 <= level <= 4:
                run_spec = _heading_spec_for_level(spec, level) or spec.body
                para_spec = run_spec
                is_heading = True
            elif detected_role == "tail_meta":
                run_spec = spec.tail_meta or spec.body
                para_spec = spec.tail_meta or spec.body
            else:
                run_spec = spec.body
                para_spec = spec.body

            if run_spec is None and para_spec is None:
                skipped += 1
                continue
            _apply_role_format_to_paragraph(node, run_spec, para_spec)
            if is_heading and spec.apply_outline_level and isinstance(level, int) and 1 <= level <= 4:
                apply_heading_outline_level(node, level)
            if is_heading:
                heading_applied += 1
            else:
                body_applied += 1
        elif isinstance(node, Table):
            _apply_role_format_to_table(node, spec.table_text or spec.body, spec.body)
            table_applied += 1
        else:
            #文本框、绘图画布等无法安全格式化的元素原样保留
            skipped += 1

    after_len = _document_text_length(nodes)
    after_texts = _document_text_snapshot(nodes)
    changed_indexes = [
        index for index, (before, after) in enumerate(zip(before_texts, after_texts))
        if before is not None and before != after
    ]
    if changed_indexes:
        #承诺过一字不改，出现改动即失败：返回0让上层不落盘
        details = "；".join(
            f"第{index}个元素：“{_clip_text(before_texts[index])}…” -> “{_clip_text(after_texts[index])}…”"
            for index in changed_indexes[:5]
        )
        message = (f"文字保全校验失败：{len(changed_indexes)} 个元素的文字被改动"
                   f"（原文 {before_len} 字 -> {after_len} 字），已放弃保存排版结果。{details}")
        print(f"【格式规范】{message}")
        return 0, message

    print(f"【格式规范】文字保全校验通过：{len(before_texts)} 个元素逐元素比对一致（全文 {after_len} 字）")

    applied = heading_applied + body_applied + table_applied
    print(f"【格式规范】完成：标题 {heading_applied} 处，正文 {body_applied} 处，表格 {table_applied} 张"
          + (f"，跳过 {skipped} 处" if skipped else ""))
    return applied, ""


# ================== 复合指令：有序步骤清单的落地执行 ==================

STEP_MODE_LABELS = {
    "replace": "模式二：全局查找替换",
    "patch": "模式一：局部微调",
    "global": "模式三：全局重构与润色",
    "format": "模式四：格式规范化",
}

#replace 只换词、不改变结构，排最前；局部增补（patch）先于整篇重写（global）；format 只写排版属性，必须最后
STEP_MODE_RANK = {"replace": 0, "patch": 1, "global": 2, "format": 3}

#指令含这些词说明用户想增删文字，而 format 模式不改文字，仅用于给出提示
CONTENT_CHANGE_HINTS = ("增加", "添加", "新增", "补充", "加入", "增添", "删去", "删除")


def normalize_rewrite_steps(raw_steps, fallback_mode: str) -> Tuple[List[dict], List[str]]:
    """把意图节点给出的步骤清单校正为可依次执行的顺序，返回 (步骤列表, 调整说明)。

    硬性依赖关系（大模型排错时由程序纠正，而不是直接失败）：
    1. replace 只换词、不改变元素数量，必须排在最前；
    2. 局部增补 patch 先于整篇重写 global（先补内容，再由全局重塑统一行文）；
    3. format 只写排版属性，必须在内容定稿之后执行，否则新增段落不会被规范化；
    4. 多个 replace 或 format 步骤语义上等价，合并为一步；global 会整篇重写，重复的只保留第一个；
    5. 只做排序与合并，不丢弃任何一类诉求（patch 与 global 同时出现时会依次执行）。

    模型未给出步骤时退化为单步骤（沿用 state['rewrite_mode'] 的旧行为）。
    """
    steps: List[dict] = []
    notes: List[str] = []
    for position, raw in enumerate(list(raw_steps or [])):
        if not isinstance(raw, dict):
            continue
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in STEP_MODE_RANK:
            continue
        try:
            order = int(raw.get("order") or position + 1)
        except (TypeError, ValueError):
            order = position + 1
        steps.append({
            "order": order,
            "mode": mode,
            "target": str(raw.get("target") or "").strip(),
            "instruction": str(raw.get("instruction") or "").strip(),
            "replace_pairs": dict(raw.get("replace_pairs") or {}),
        })
    if not steps:
        mode = (fallback_mode or "patch").strip().lower()
        if mode not in STEP_MODE_RANK:
            mode = "patch"
        steps = [{"order": 1, "mode": mode, "target": "", "instruction": "", "replace_pairs": {}}]
        return steps, notes

    steps.sort(key=lambda step: step["order"])
    if len(steps) > 1:
        for mode, label, note in (
            ("replace", "全局查找替换", "已合并为一次全局查找替换（词对取并集）"),
            ("format", "格式规范化", "已合并为一次格式规范化"),
            ("global", "全局重构与润色", "全局重塑会整篇重写，重复的只保留第一个"),
        ):
            same = [step for step in steps if step["mode"] == mode]
            if len(same) <= 1:
                continue
            merged_pairs: Dict[str, str] = {}
            for step in same:
                merged_pairs.update(step["replace_pairs"])
            same[0]["replace_pairs"] = merged_pairs
            dropped = {id(step) for step in same[1:]}
            steps = [step for step in steps if id(step) not in dropped]
            notes.append(f"检测到 {len(same)} 个{label}步骤，{note}")

        ordered = sorted(steps, key=lambda step: STEP_MODE_RANK[step["mode"]])
        if [step["mode"] for step in ordered] != [step["mode"] for step in steps]:
            notes.append("已按依赖关系重排执行顺序：" + " → ".join(STEP_MODE_LABELS[step["mode"]] for step in ordered))
        steps = ordered

    for position, step in enumerate(steps, 1):
        step["order"] = position
    return steps, notes


def build_step_dialog(dialog: str, step: dict, total_steps: int) -> str:
    """多步骤执行时给该步的提示词附加作用域说明，避免各步互相越界。"""
    if total_steps <= 1:
        return dialog
    scope = f"作用范围：{step['target']}；" if step.get("target") else ""
    requirement = step.get("instruction") or "按用户整体要求完成本步骤"
    return (
        f"{dialog}\n"
        f"【本次只执行第 {step['order']} 步（共 {total_steps} 步）】执行模式：{STEP_MODE_LABELS[step['mode']]}；"
        f"{scope}本步要求：{requirement}\n"
        "其它诉求由系统在后续步骤中单独执行，本步不要越界处理不属于本步骤的内容。"
    )


def dispatch_rewrite_mode(llm, mode: str, dialog: str, retrieved_info: str, doc: Document,
                          nodes, base_items, description, replace_pairs,
                          step_no: int = 0, total_steps: int = 1,
                          target: str = "") -> Tuple[int, str]:
    """按模式调用对应改写引擎；多步骤时在日志中标出步骤序号。"""
    prefix = f"步骤{step_no}/{total_steps}：" if total_steps > 1 else ""
    if mode == "replace":
        print(f"【改写调度】{prefix}{STEP_MODE_LABELS['replace']}")
        return run_replace_rewrite(llm, dialog, doc, replace_pairs)
    if mode == "global":
        print(f"【改写调度】{prefix}{STEP_MODE_LABELS['global']}")
        return run_global_rewrite(llm, dialog, retrieved_info, doc, nodes, base_items, description)
    if mode == "format":
        #格式规范化是整篇排版：作用域只写在提示词里无法约束实际循环，故拒绝局部范围，避免"只排版某章"变成排版全文
        if (target or "").strip():
            return 0, (f"格式规范化暂不支持指定局部范围（本步范围：{target.strip()}），"
                       "请以整篇文档为单位进行排版规范化")
        print(f"【改写调度】{prefix}{STEP_MODE_LABELS['format']}")
        return run_format_rewrite(llm, dialog, retrieved_info, doc, nodes, base_items, description)
    print(f"【改写调度】{prefix}{STEP_MODE_LABELS['patch']}")
    return run_patch_rewrite(llm, dialog, retrieved_info, doc, nodes, base_items, description)


def create_rewrite_node(llm):
    """创建改写节点，按 state['rewrite_steps'] 的有序步骤依次调度四层改写引擎。"""
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

        # 有序步骤清单：复合指令（改内容 + 规范排版 + 查找替换…）由大模型拆步，这里按依赖顺序逐步落地
        steps, notes = normalize_rewrite_steps(state.get("rewrite_steps"), mode)
        for note in notes:
            print(f"【步骤编排】{note}")
        if len(steps) > 1:
            print(f"【改写编排】共 {len(steps)} 个步骤，将按序执行：")
            for step in steps:
                scope = f"范围：{step['target']}｜" if step["target"] else ""
                print(f"   步骤{step['order']}：{STEP_MODE_LABELS[step['mode']]}｜{scope}要求：{step['instruction'] or '（沿用整体要求）'}")
        elif steps[0]["mode"] == "format":
            hints = [word for word in CONTENT_CHANGE_HINTS if word in dialog]
            if hints:
                print(
                    "【步骤编排】注意：本次为「格式规范化」单步骤（只调排版、不改文字），"
                    f"但指令中出现增删内容类词语 {hints}；若还需新增或删除内容，请单独说明。"
                )

        replace_pairs = dict(state.get("replace_pairs") or {})
        applied = 0
        error = ""
        failed_step = 0
        for position, step in enumerate(steps, 1):
            if position > 1:
                #上一步可能已增删或替换元素，必须重新解析文档结构与格式快照，否则后续步骤下标错位、新段落不会被排版
                nodes, base_items, description = build_document_description(doc)
                print(f"【步骤编排】已重新解析文档结构：共 {len(nodes)} 个元素")
            step_pairs = dict(step["replace_pairs"] or replace_pairs) if step["mode"] == "replace" else replace_pairs
            step_applied, error = dispatch_rewrite_mode(
                llm, step["mode"], build_step_dialog(dialog, step, len(steps)), retrieved_info,
                doc, nodes, base_items, description, step_pairs,
                step_no=position, total_steps=len(steps), target=step["target"],
            )
            applied += step_applied or 0
            if error:
                failed_step = position
                print(f"【步骤编排】第{position}步（{STEP_MODE_LABELS[step['mode']]}）失败：{error}")
                break

        if error and applied == 0:
            #没有任何改动落地（含单步骤失败）：与旧行为一致，直接报错、不保存
            return {**state, "state": "error", "error": error}

        if applied == 0:
            print("未产生任何有效修改，文档内容保持不变")

        if state.get("save_path"):
            save_path = resolve_save_path(state["save_path"])
        else:
            save_path = Path(rewrite_path)

        save_path = save_document(doc, save_path)  #目标被占用时会避让另存并返回实际路径
        print("改写后的文档已保存到：", str(save_path))
        if error:
            #多步执行中途失败：已完成的部分修改仍然保存，避免前功尽弃
            if failed_step and len(steps) > 1:
                error = f"第{failed_step}步执行失败（该步之前的修改已保存）：{error}"
            return {
                **state,
                "state": "error",
                "error": error,
                "save_path": str(save_path),
            }
        return {**state, "state": "succeeded", "save_path": str(save_path)}

    return rewrite_node
