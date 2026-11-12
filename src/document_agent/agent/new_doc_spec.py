# -*- coding: utf-8 -*-
"""新建文档约束的结构化模型与确定性兜底解析。

模型负责泛化理解，本模块负责：
1. 把模型结果规整成 NewDocSpec；
2. 在模型缺失/旧 checkpoint/单测直调时，从用户原话解析常见约束；
3. 合并模型与兜底结果，避免把约束提取散落成一堆关键词。
"""
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

TextStyleScope = Literal["all", "body", "headings", "table"]
CountingRule = Literal["non_whitespace", "chinese_chars", "all_chars"]
DocumentForm = Literal["prose", "sectioned", "field_list", "table", "form"]


class TextStyleSpec(BaseModel):
    font_name: Optional[str] = Field(
        default=None, description="字体名称，如宋体、黑体、楷体、微软雅黑；None 表示未指定。"
    )
    font_color: Optional[str] = Field(
        default=None, description="字体颜色，六位十六进制，如 000000；None 表示未指定。"
    )
    scope: TextStyleScope = Field(
        default="all",
        description="作用域：all=全文，body=正文，headings=总标题/章节标题，table=表格文字。",
    )


TitleAlignment = Literal["center", "left", "right", "justify"]


class TitleStyleSpec(BaseModel):
    """文档总标题的显式排版要求。

    未填写的字段回落到默认值（22 磅 / 加粗 / 居中 / 段后 12 磅），
    一旦用户提出要求就以用户为准；与章节标题（headings 作用域）互不影响。
    """

    font_name: Optional[str] = Field(default=None, description="字体名称，如宋体、黑体；None=未指定。")
    font_color: Optional[str] = Field(default=None, description="六位十六进制颜色，如 000000；None=未指定。")
    font_size: Optional[float] = Field(
        default=None, ge=6, le=100, description="字号（磅）；None=未指定。"
    )
    bold: Optional[bool] = Field(default=None, description="是否加粗；None=未指定。")
    alignment: Optional[TitleAlignment] = Field(
        default=None, description="对齐方式：center/left/right/justify；None=未指定。"
    )
    space_before: Optional[float] = Field(
        default=None, ge=0, le=100, description="段前间距（磅）；None=未指定。"
    )
    space_after: Optional[float] = Field(
        default=None, ge=0, le=100, description="段后间距（磅）；None=未指定。"
    )


class NewDocSpec(BaseModel):
    title: Optional[str] = Field(
        default=None, description="用户明确指定的总标题；未指定为 null，不得自行推断。"
    )
    title_style: Optional[TitleStyleSpec] = Field(
        default=None,
        description="用户对总标题的字体/字号/加粗/对齐/段距要求；未提出为 null，由系统按默认版式生成。",
    )
    max_body_chars: Optional[int] = Field(
        default=None, description="正文长度上限；未指定为 null。"
    )
    counting_rule: CountingRule = Field(
        default="non_whitespace",
        description="字数口径：non_whitespace=非空白字符，chinese_chars=汉字，all_chars=全部字符。",
    )
    count_include_title: bool = Field(
        default=False,
        description=(
            "字数上限是否把总标题算进去。用户写“全文（包含总标题）不超过N字”为 true；"
            "写“正文最多N字，不含文档标题”或未提及时为 false。"
        ),
    )
    table_policy: Literal["allow", "forbid", "unspecified"] = Field(
        default="unspecified", description="表格策略：允许/禁止/未指定。"
    )
    text_styles: List[TextStyleSpec] = Field(
        default_factory=list, description="按作用域声明的字体/颜色要求。"
    )
    required_topics: List[str] = Field(
        default_factory=list, description="用户明确要求覆盖的主题/字段，用于修复提示和完整性提示。"
    )
    duplicate_policy: Literal["strict", "normal", "off"] = Field(
        default="normal", description="重复检查强度：strict=强制检查，normal=有结构约束时检查，off=不检查。"
    )
    document_form: Optional[DocumentForm] = Field(
        default=None,
        description="文档形态：prose 连续短文、sectioned 分章节说明、field_list 条款清单、table 表格、form 表单；未明确为 null。",
    )
    include_section_titles: Optional[bool] = Field(
        default=None, description="是否要求包含章节标题；未明确为 null。"
    )
    include_field_names: Optional[bool] = Field(
        default=None, description="是否要求包含字段名称；未明确为 null。"
    )


FONT_NAMES = (
    "微软雅黑", "Times New Roman", "DengXian", "Calibri", "Arial",
    "宋体", "黑体", "楷体", "仿宋", "等线", "隶书", "幼圆",
)
COLOR_NAMES = {
    "黑色": "000000",
    "红色": "FF0000",
    "蓝色": "0000FF",
    "绿色": "00B050",
    "灰色": "808080",
    "白色": "FFFFFF",
}
NO_TABLE_TOKENS = (
    "不用表格", "不要表格", "无表格", "无需表格", "不使用表格", "不采用表格", "不得使用表格",
    "禁止表格", "不允许表格", "不允许出现表格", "不要出现表格", "不得出现表格",
    "不需要表格", "不推荐表格", "只用段落", "仅用段落", "纯段落", "表格数量为0", "不要用table",
)
ALLOW_TABLE_TOKENS = (
    "允许表格", "可以使用表格", "可以用表格", "需要表格", "用表格呈现", "表格形式",
)
NEGATED_TABLE_PATTERN = re.compile(
    r"(?:不要|不用|禁止|不得|不允许|无需|不需要|不推荐|严禁|勿|别)"
    r"([^。；;\n]{0,6}?)(?:表格|table)",
    re.I,
)
DOC_TITLE_VERB_PATTERN = re.compile(
    r"(?:写|编写|生成|输出|新建|起草|撰写|创建)\s*《([^》]+)》"
)
DOC_TITLE_LABEL_PATTERN = re.compile(
    r"(?:文档标题|总标题|标题|题目)\s*(?:为|是|：|:)?\s*《([^》]+)》"
)
DOC_TITLE_LABEL_PLAIN_PATTERN = re.compile(
    r"(?:文档标题|总标题|标题|题目)\s*(?:为|是|：|:)\s*([^，,。；;、\n]+)"
)
DOC_TITLE_AS_PATTERN = re.compile(
    r"(?:以\s*)?《([^》]+)》\s*(?:为|作为)\s*(?:文档)?(?:标题|题目)"
)
LIMIT_VERB_PATTERN = (
    r"(?:最多|不超过|不得超过|不要超过|别超过|不多于|不超|至多|"
    r"≤|<=|控制在|限制在|上限为|上限|封顶)"
)
BODY_LIMIT_PATTERN = re.compile(
    r"(?:正文|全文|全篇|文档正文|正文内容|正文字数)"
    r"[^。；;\n]{0,16}?"
    + LIMIT_VERB_PATTERN
    + r"\s*(\d+)\s*个?\s*(非空白字符|字符|汉字|字)"
)
ONLY_LIMIT_PATTERN = re.compile(
    LIMIT_VERB_PATTERN + r"\s*(\d+)\s*个?\s*(非空白字符|字符|汉字|字)"
)
WITHIN_LIMIT_PATTERN = re.compile(
    r"(\d+)\s*个?\s*(非空白字符|字符|汉字|字)\s*(?:以内|以下|之内)"
)
CLAUSE_SPLIT_PATTERN = re.compile(r"[，,；;。\n、]")
SCOPE_TOKENS = (
    # 具体作用域优先，避免“表格文字”“章节标题文字”被 all 的裸“文字”抢先命中。
    ("headings", ("章节标题", "总标题", "标题", "各级标题")),
    ("table", ("表格文字", "表格内容", "表格")),
    ("body", ("正文", "段落", "主体文字")),
    ("all", ("全部文字", "所有文字", "文字", "全文", "全篇", "整篇", "全部内容", "所有内容")),
)
TITLE_SECTION_PREFIXES = (
    "章节", "小节", "本章", "各章", "每章", "各级", "一级", "二级", "三级", "下级",
    "副", "小", "子",
)
TITLE_REJECT_VALUES = {"无", "暂无", "没有", "不指定", "不写", "无标题", "空", "none", "null"}
SECTION_NUMBER_PATTERN = re.compile(
    r"^[（(]?(?:[一二三四五六七八九十百零]+|\d+)[、.．)）]?$"
)
MAX_TITLE_LENGTH = 60
TITLE_TAIL_STOP_PATTERN = re.compile(
    r"\s+(?:正文|全文|字数|格式|要求|包含|不要|不用|禁止|不得|另存为)"
)
LIMIT_CONTEXT_FORBIDDEN = (
    "标题", "每项", "每个", "每条", "每章", "章节", "字段", "条目", "表格",
    "摘要", "概述", "总览", "导语", "目录", "封面", "页眉", "页脚", "脚注",
)
LIMIT_CONTEXT_BODY = ("正文", "全文", "全篇")
LIMIT_CLAUSE_SEPARATORS = ("。", "；", ";", "\n", "，", ",")
NEGATION_PATTERN = re.compile(
    r"(?:不要|不用|不使用|不采用|不出现|禁止|不得|不允许|不推荐|避免|勿|别|"
    r"严禁|无需|不需要)"
)
TOPIC_HEAD_PATTERN = re.compile(
    r"(?:^|[，,；;。\n])(?:请|并|需|同时|另外|还)?"
    r"(?:说明|写清|写明确|包含|包括|涵盖|"
    r"只回答(?:[\u4e00-\u9fa5\d]+个)?(?:字段|方面)?\s*[:：]?|字段\s*[:：]?)"
)
TOPIC_STOP_PATTERN = re.compile(
    r"(?:正文最多|正文不超过|正文≤|正文<=|不用表格|不要表格|禁止表格|"
    r"不能混用|不得混用|不检索知识库|另存为|全部文字)"
)
META_TOPICS = {"字段名称", "章节标题", "文档标题", "总标题", "标题", "正文", "格式要求"}
TITLE_BOOK_PATTERN = re.compile(r"《([^》]+)》")
TITLE_REFERENCE_PREFIXES = (
    "参考", "依据", "来源", "资料", "基于", "按照", "根据", "章节", "小节", "本章", "各章",
)
TITLE_HINT_PREFIX_TOKENS = (
    "写", "编写", "生成", "输出", "新建", "起草", "撰写", "创建",
    "标题", "题目", "命名", "取名", "叫", "弄", "做", "整", "来", "一份",
)
TITLE_HINT_SUFFIX_TOKENS = (
    "做标题", "作为标题", "当标题", "为标题", "是标题", "文档标题", "总标题",
)
LIMIT_HINT_PATTERN = re.compile(
    r"(?:字数|篇幅)[^。；;\n]{0,12}?"
    r"(?:不超过|不得超过|不要超过|别超过|最多|至多|封顶|上限|控制在|限制在|以内|以下)|"
    r"(?:全文|全篇|正文)[^。；;\n]{0,12}?"
    r"(?:不超过|不得超过|不要超过|别超过|最多|至多|封顶|上限|控制在|限制在)"
    r"[^。；;\n]{0,8}?(?:\d+|[一二三四五六七八九十百千万零两]+)\s*个?\s*"
    r"(?:非空白字符|字符|汉字|字|页)"
)
TABLE_HINT_PATTERN = re.compile(
    r"(?:不要|不用|禁止|不得|不允许|避免|尽量少用|别|代替|替代)"
    r"[^。；;\n]{0,8}?(?:表格|table)|"
    r"(?:最多|不超过)[^。；;\n]{0,6}?(?:表格|表)(?=[，,。；;\s]|$)|"
    r"(?:表格)[^。；;\n]{0,6}?(?:最多|不超过)"
)
STYLE_HINT_PATTERN = re.compile(
    r"(?:字体|标题|正文|全文|全篇|表格|文字)\s*"
    r"(?:用|改成|改为|设置为|统一为|统一成|设为|是|为)\s*"
    r"([\u4e00-\u9fffA-Za-z]{1,12}(?:体|雅黑|色|宋|黑|楷))"
)
DUPLICATE_HINT_PATTERN = re.compile(
    r"(?:不要重复|不得重复|只写一次|不要总览|不要摘要|不要概述|避免重复|别啰嗦|车轱辘)"
)
SCOPE_COMPLETENESS_TOKENS = ("统一", "全部", "所有", "全文", "全篇", "整篇")
FONT_BOUNDARY_CHARS = set(" \t\r\n，,。；;：:、用为成是改设选称")
FONT_SCOPE_PREFIXES = {
    "中文", "正文", "文字", "内容", "标题", "题目", "全文", "全篇", "整篇", "通篇",
    "表格", "所有", "全部", "字体", "统一", "一律", "整体", "颜色", "色彩", "字号", "名称",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _strip_title_quotes(value: str) -> str:
    return (value or "").strip().strip("“”\"'《》").strip()


def _looks_like_section_title_label(text: str, start: int) -> bool:
    """排除“章节标题：…”“小节标题：…”这类章节级标签。"""
    prefix = text[max(0, start - 4):start]
    return any(token in prefix for token in TITLE_SECTION_PREFIXES)


def _is_valid_title(title: str) -> bool:
    """过滤空标题、占位词和纯章节序号，避免把“一”“无”当总标题。"""
    value = (title or "").strip()
    if not value or len(value) > MAX_TITLE_LENGTH:
        return False
    if value.lower() in TITLE_REJECT_VALUES:
        return False
    if SECTION_NUMBER_PATTERN.match(value):
        return False
    return True


def extract_doc_title(text: str) -> Optional[str]:
    """提取用户指定的文档标题；参考资料文件名和章节级标题不算总标题。"""
    text = text or ""
    for pattern in (DOC_TITLE_VERB_PATTERN, DOC_TITLE_LABEL_PATTERN, DOC_TITLE_AS_PATTERN):
        for match in pattern.finditer(text):
            if pattern is DOC_TITLE_LABEL_PATTERN and _looks_like_section_title_label(
                text, match.start()
            ):
                continue
            title = _strip_title_quotes(match.group(1))
            if _is_valid_title(title) and not _looks_like_reference_file(title):
                return title
    for match in DOC_TITLE_LABEL_PLAIN_PATTERN.finditer(text):
        if _looks_like_section_title_label(text, match.start()):
            continue
        title = _strip_title_quotes(match.group(1)).strip()
        title = TITLE_TAIL_STOP_PATTERN.split(title, maxsplit=1)[0].strip()
        if _is_valid_title(title) and not _looks_like_reference_file(title):
            return title
    return None


def _looks_like_reference_file(value: str) -> bool:
    return bool(re.search(r"\.(txt|docx|doc|pdf|xlsx|xls|json|csv|md)$", value, re.I))


def _count_rule_from_unit(unit: str) -> CountingRule:
    if unit == "非空白字符":
        return "non_whitespace"
    if unit in ("汉字", "字"):
        return "chinese_chars"
    return "non_whitespace"


def _limit_clause(text: str, start: int, end: int) -> str:
    clause_start = max(text.rfind(sep, 0, start) for sep in LIMIT_CLAUSE_SEPARATORS) + 1
    return text[clause_start:end]


def _limit_clause_allows(clause: str) -> bool:
    """只有正文/全文语境或没有其他对象限定的“N字以内”才算全文字数上限。"""
    if any(token in clause for token in LIMIT_CONTEXT_BODY):
        return True
    return not any(token in clause for token in LIMIT_CONTEXT_FORBIDDEN)


def extract_body_limit(text: str):
    """提取正文上限和计数口径；找不到返回 (None, non_whitespace)。"""
    text = text or ""
    match = BODY_LIMIT_PATTERN.search(text)
    if not match:
        # 没有“正文/全文”前缀时，避免把“标题最多20字”“每项不超过20字”误当成正文上限。
        for candidate in ONLY_LIMIT_PATTERN.finditer(text):
            if not _limit_clause_allows(_limit_clause(text, candidate.start(), candidate.end())):
                continue
            match = candidate
            break
    if not match:
        for candidate in WITHIN_LIMIT_PATTERN.finditer(text):
            if not _limit_clause_allows(_limit_clause(text, candidate.start(), candidate.end())):
                continue
            match = candidate
            break
    if not match:
        return None, "non_whitespace"
    return int(match.group(1)), _count_rule_from_unit(match.group(2))


TITLE_COUNT_EXCLUDE_PATTERN = re.compile(
    r"(?:不包含|不含|不包括|不计入|不算|排除|不将)"
    r"[^。；;\n]{0,8}?(?:总标题|文档标题|标题)"
)
TITLE_COUNT_INCLUDE_PATTERN = re.compile(
    r"(?:包含|含|包括|连同|计入|算上|算入)"
    r"[^。；;\n]{0,8}?(?:总标题|文档标题)"
)


def extract_count_scope(text: str) -> Optional[bool]:
    """判断字数上限是否包含总标题。

    只认“总标题/文档标题”这类无歧义说法，避免把“章节标题”“字段名称”误当成标题口径；
    出现“不含/不包括”等否定说法时一律按不包含处理。
    """
    text = text or ""
    if TITLE_COUNT_EXCLUDE_PATTERN.search(text):
        return False
    if TITLE_COUNT_INCLUDE_PATTERN.search(text):
        return True
    return None


def extract_table_policy(text: str) -> Literal["allow", "forbid", "unspecified"]:
    compact = re.sub(r"\s+", "", text or "").lower()
    if any(token.lower() in compact for token in NO_TABLE_TOKENS):
        return "forbid"
    for match in NEGATED_TABLE_PATTERN.finditer(compact):
        gap = match.group(1)
        # “不要超过3个表格”是数量限制，不是禁用表格，不能按 forbid 处理。
        if re.search(r"\d|超过|多于|至少|少于|不少于", gap):
            continue
        return "forbid"
    for token in ALLOW_TABLE_TOKENS:
        index = compact.find(token.lower())
        if index < 0:
            continue
        prefix = compact[max(0, index - 8):index]
        if NEGATION_PATTERN.search(prefix):
            continue
        return "allow"
    return "unspecified"


def _scope_from_clause(clause: str) -> Optional[TextStyleScope]:
    for scope, tokens in SCOPE_TOKENS:
        if any(token in clause for token in tokens):
            return scope
    return None


def _style_match_is_standalone(clause: str, index: int) -> bool:
    """避免把“思源黑体”“深灰色”降级成白名单里的“黑体”“灰色”。"""
    if index <= 0:
        return True
    prefix = clause[max(0, index - 2):index]
    # 否定项仍要作为“提到过该约束”记录下来，交给 _is_negated_at 过滤并传递作用域。
    if NEGATION_PATTERN.search(prefix):
        return True
    if prefix[-1] in FONT_BOUNDARY_CHARS:
        return True
    return prefix in FONT_SCOPE_PREFIXES


def _find_font(clause: str):
    """返回 (字体名, 起始位置)；同句多字体时取最先出现的独立字体。"""
    lowered = clause.lower()
    hit = None
    for font in FONT_NAMES:
        start = 0
        while True:
            index = lowered.find(font.lower(), start)
            if index < 0:
                break
            if _style_match_is_standalone(clause, index):
                if hit is None or index < hit[1]:
                    hit = (font, index)
                break
            start = index + 1
    return hit


def _find_color(clause: str):
    """返回 (颜色值, 起始位置)；命名颜色与 #RRGGBB 取最先出现的独立颜色。"""
    hit = None
    for name, value in COLOR_NAMES.items():
        start = 0
        while True:
            index = clause.find(name, start)
            if index < 0:
                break
            if _style_match_is_standalone(clause, index):
                if hit is None or index < hit[1]:
                    hit = (value, index)
                break
            start = index + 1
    hex_match = re.search(r"#([0-9A-Fa-f]{6})", clause)
    if hex_match and (hit is None or hex_match.start(1) < hit[1]):
        hit = (hex_match.group(1).upper(), hex_match.start(1))
    return hit


def _is_negated_at(clause: str, position: int) -> bool:
    prefix = clause[max(0, position - 5):position]
    return bool(NEGATION_PATTERN.search(prefix))


def merge_text_styles(styles: List[TextStyleSpec]) -> List[TextStyleSpec]:
    """同作用域的 font/color 合并，后出现的非空字段覆盖前面的。"""
    merged: Dict[str, TextStyleSpec] = {}
    for style in styles:
        current = merged.get(style.scope)
        if current is None:
            merged[style.scope] = TextStyleSpec(
                font_name=style.font_name, font_color=style.font_color, scope=style.scope
            )
            continue
        if style.font_name:
            current.font_name = style.font_name
        if style.font_color:
            current.font_color = style.font_color
    return [merged[key] for key in ("all", "body", "headings", "table") if key in merged]


def extract_text_styles(text: str) -> List[TextStyleSpec]:
    """从用户原话解析字体/颜色与作用域，支持“标题黑体、正文宋体”。"""
    text = text or ""
    styles: List[TextStyleSpec] = []
    inherited_scope: Optional[TextStyleScope] = None
    for clause in CLAUSE_SPLIT_PATTERN.split(text):
        clause = clause.strip()
        if not clause:
            continue
        # “总标题用黑体”这类要求由 title_style 承载；若同时点名章节标题才继续按 headings 处理
        if any(marker in clause for marker in TITLE_STYLE_MARKERS) and not any(
            word in clause
            for word in ("章节标题", "各级标题", "所有标题", "一级标题", "二级标题", "三级标题")
        ):
            continue
        scope = _scope_from_clause(clause) or inherited_scope
        font_hit = _find_font(clause)
        color_hit = _find_color(clause)
        had_requirement = bool(font_hit or color_hit)
        if font_hit and _is_negated_at(clause, font_hit[1]):
            font_hit = None
        if color_hit and _is_negated_at(clause, color_hit[1]):
            color_hit = None
        if not font_hit and not color_hit:
            # 否定项本身不算要求，但它声明的作用域要继续向后传递。
            if had_requirement and scope:
                inherited_scope = scope
            continue
        if scope:
            inherited_scope = scope
        elif not inherited_scope:
            # 未指明作用域且不含“文字/全文/全部”时按正文，避免误改标题。
            scope = "body"
            inherited_scope = scope
        styles.append(TextStyleSpec(
            font_name=font_hit[0] if font_hit else None,
            font_color=color_hit[0] if color_hit else None,
            scope=scope,
        ))
    return merge_text_styles(styles)


# 只认“总标题/文档标题/题目/大标题”，不认裸“标题”，避免把章节标题要求当成总标题
TITLE_STYLE_MARKERS = ("总标题", "文档标题", "题目", "大标题")
CN_FONT_SIZES = {
    "初号": 42.0, "小初": 36.0, "一号": 26.0, "小一": 24.0,
    "二号": 22.0, "小二": 18.0, "三号": 16.0, "小三": 15.0,
    "四号": 14.0, "小四": 12.0, "五号": 10.5, "小五": 9.0, "六号": 7.5,
}
_TITLE_STYLE_CLAUSE_SPLIT = re.compile(r"[，,；;。\n]")
_NUMBER_PT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*磅")
_CN_SIZE_PATTERN = re.compile("|".join(sorted(CN_FONT_SIZES, key=len, reverse=True)))
_NO_BOLD_PATTERN = re.compile(r"(?:不|不要|无需|不用|非|别)\s*加粗")
_BOLD_PATTERN = re.compile(r"加粗|加黑")
_ALIGN_PATTERNS = (
    (re.compile(r"两端对齐"), "justify"),
    (re.compile(r"(?:居中对齐|居中)"), "center"),
    (re.compile(r"(?:左对齐|靠左)"), "left"),
    (re.compile(r"(?:右对齐|靠右)"), "right"),
)
_SPACE_AFTER_PATTERN = re.compile(r"段后\s*(\d+(?:\.\d+)?)\s*磅")
_SPACE_BEFORE_PATTERN = re.compile(r"段前\s*(\d+(?:\.\d+)?)\s*磅")


def extract_title_style(text: str) -> Optional[TitleStyleSpec]:
    """解析用户对“总标题”的排版要求；没写或只写章节标题时返回 None。

    只处理含“总标题/文档标题/题目/大标题”的短句，“一级标题16磅”这类章节标题
    要求不会被误当成总标题；一个短句内的多项要求（字体、字号、加粗、对齐、段距）
    可以同时解析。
    """
    text = text or ""
    merged: Dict[str, object] = {}
    found = False
    for clause in _TITLE_STYLE_CLAUSE_SPLIT.split(text):
        clause = clause.strip()
        if not clause or not any(marker in clause for marker in TITLE_STYLE_MARKERS):
            continue
        found = True
        font_hit = _find_font(clause)
        if font_hit and not _is_negated_at(clause, font_hit[1]):
            merged.setdefault("font_name", font_hit[0])
        color_hit = _find_color(clause)
        if color_hit and not _is_negated_at(clause, color_hit[1]):
            merged.setdefault("font_color", color_hit[0])
        size_hit = _NUMBER_PT_PATTERN.search(clause)
        if size_hit:
            merged.setdefault("font_size", float(size_hit.group(1)))
        elif _CN_SIZE_PATTERN.search(clause):
            merged.setdefault("font_size", CN_FONT_SIZES[_CN_SIZE_PATTERN.search(clause).group(0)])
        if _NO_BOLD_PATTERN.search(clause):
            merged.setdefault("bold", False)
        elif _BOLD_PATTERN.search(clause):
            merged.setdefault("bold", True)
        for pattern, alignment in _ALIGN_PATTERNS:
            if pattern.search(clause):
                merged.setdefault("alignment", alignment)
                break
        space_after = _SPACE_AFTER_PATTERN.search(clause)
        if space_after:
            merged.setdefault("space_after", float(space_after.group(1)))
        space_before = _SPACE_BEFORE_PATTERN.search(clause)
        if space_before:
            merged.setdefault("space_before", float(space_before.group(1)))
    if not found or not merged:
        return None
    return TitleStyleSpec(**merged)


def extract_required_topics(text: str) -> List[str]:
    """提取“说明/写清/包含/只回答...”之后列出的主题，仅供提示和完整性观察。"""
    topics: List[str] = []
    for sentence in re.split(r"[。；;\n]", text or ""):
        sentence = sentence.strip()
        if not sentence:
            continue
        match = TOPIC_HEAD_PATTERN.search(sentence)
        if not match:
            continue
        body = sentence[match.end():].strip(" ：:")
        body = TOPIC_STOP_PATTERN.split(body, maxsplit=1)[0]
        for item in re.split(r"、|，|,|和|及|与", body):
            item = item.strip(" 。；;：:")
            item = re.sub(r"^(?:新版|旧版|完整|具体|相关)", "", item)
            if item in META_TOPICS:
                continue
            if 1 < len(item) <= 30:
                topics.append(item)
    return list(dict.fromkeys(topics))


def extract_duplicate_policy(text: str) -> Literal["strict", "normal", "off"]:
    text = text or ""
    if any(token in text for token in ("不要重复", "不得重复", "只写一次", "不要总览",
                                      "不要摘要", "不要概述", "避免重复")):
        return "strict"
    return "normal"


def extract_document_form(
    text: str,
) -> tuple[Optional[DocumentForm], Optional[bool], Optional[bool]]:
    """确定性识别文档形态、章节标题与字段名称要求；未明确返回 None。"""
    text = text or ""
    has_prose = any(token in text for token in (
        "连续短文", "连续段落", "一段话", "一整段", "不分章节", "不分子章节",
    ))
    has_section = any(token in text for token in (
        "含章节标题", "包含章节标题", "有章节标题", "分章节", "按章节",
    ))
    no_section = any(token in text for token in (
        "不要章节标题", "不含章节标题", "无章节标题", "不要分章节", "不分章节",
    ))
    has_field_names = any(token in text for token in (
        "含字段名称", "包含字段名称", "有字段名称", "字段名称", "字段名",
    ))
    no_field_names = any(token in text for token in (
        "不要字段名称", "不含字段名称", "不要字段名", "不写字段名",
    ))
    has_list = any(token in text for token in (
        "字段清单", "字段列表", "逐条列明", "逐条列出", "按字段列出",
        "条款清单", "清单形式",
    ))
    has_form = any(token in text for token in ("表单", "填写表", "表格式填写"))
    has_table = any(token in text for token in (
        "表格呈现", "用表格", "表格形式", "表格式", "表格列出",
    )) and extract_table_policy(text) != "forbid"

    if has_table:
        form: Optional[DocumentForm] = "table"
    elif has_form:
        form = "form"
    elif has_list:
        form = "field_list"
    elif has_prose and has_section:
        form = "sectioned"
    elif has_prose:
        form = "prose"
    elif has_section:
        form = "sectioned"
    else:
        form = None

    section_titles = False if no_section else (True if has_section else None)
    field_names = False if no_field_names else (True if has_field_names else None)
    return form, section_titles, field_names


def parse_new_doc_spec(text: str) -> NewDocSpec:
    """确定性兜底：覆盖常见表达；未明确要求的字段保持 null/unspecified。"""
    max_body_chars, counting_rule = extract_body_limit(text)
    document_form, include_section_titles, include_field_names = extract_document_form(text)
    return NewDocSpec(
        title=extract_doc_title(text),
        title_style=extract_title_style(text),
        max_body_chars=max_body_chars,
        counting_rule=counting_rule,
        count_include_title=bool(extract_count_scope(text)),
        table_policy=extract_table_policy(text),
        text_styles=extract_text_styles(text),
        required_topics=extract_required_topics(text),
        duplicate_policy=extract_duplicate_policy(text),
        document_form=document_form,
        include_section_titles=include_section_titles,
        include_field_names=include_field_names,
    )


def _normalize_style_hint_value(value: str) -> str:
    value = (value or "").strip()
    if value.endswith("字体"):
        value = value[:-2]
    if value in FONT_NAMES or value in COLOR_NAMES:
        return value
    if value.endswith("体") and value[:-1] in FONT_NAMES:
        return value[:-1]
    return value


def unparsed_constraint_warnings(text: str, spec: NewDocSpec) -> List[str]:
    """返回“原话有约束线索、spec 没落地”的告警，供节点日志提示。"""
    text = text or ""
    spec = spec or NewDocSpec()
    warnings: List[str] = []

    if spec.title is None:
        for match in TITLE_BOOK_PATTERN.finditer(text):
            value = _strip_title_quotes(match.group(1))
            if not value or _looks_like_reference_file(value):
                continue
            prefix = text[max(0, match.start() - 8):match.start()]
            if any(token in prefix for token in TITLE_REFERENCE_PREFIXES):
                continue
            suffix = text[match.end():match.end() + 8]
            has_prefix_hint = any(token in prefix for token in TITLE_HINT_PREFIX_TOKENS)
            has_suffix_hint = any(token in suffix for token in TITLE_HINT_SUFFIX_TOKENS)
            if not has_prefix_hint and not has_suffix_hint:
                continue
            warnings.append(f"疑似标题约束未解析：{value}")
            break

    if spec.max_body_chars is None:
        for match in LIMIT_HINT_PATTERN.finditer(text):
            clause = _limit_clause(text, match.start(), match.end())
            if not _limit_clause_allows(clause):
                continue
            warnings.append(f"疑似字数上限未解析：{match.group(0).strip()}")
            break

    if spec.table_policy == "unspecified":
        match = TABLE_HINT_PATTERN.search(text)
        if match:
            warnings.append(f"疑似表格约束未解析：{match.group(0).strip()}")

    covered_fonts = {style.font_name for style in spec.text_styles if style.font_name}
    covered_colors = {style.font_color for style in spec.text_styles if style.font_color}
    style_values = [
        _normalize_style_hint_value(match.group(1))
        for match in STYLE_HINT_PATTERN.finditer(text)
    ]
    if not style_values:
        if not spec.text_styles and (
            any(font in text for font in FONT_NAMES)
            or any(color in text for color in COLOR_NAMES)
        ):
            warnings.append("疑似字体/颜色约束未解析")
    else:
        for value in style_values:
            if value in covered_fonts or COLOR_NAMES.get(value) in covered_colors:
                continue
            if value in FONT_NAMES:
                warnings.append(f"疑似字体约束未应用：{value}")
            elif value in COLOR_NAMES:
                warnings.append(f"疑似颜色约束未应用：{value}")
            else:
                warnings.append(f"疑似未识别字体/颜色：{value}")

    if spec.text_styles:
        for clause in CLAUSE_SPLIT_PATTERN.split(text):
            if not any(token in clause for token in SCOPE_COMPLETENESS_TOKENS):
                continue
            # 只有真的在这句里要求了字体/颜色的“全文/全部”，才谈作用域是否覆盖全文；
            # “全文不超过300字”这类长度句不应触发该告警。
            if not (_find_font(clause) or _find_color(clause)):
                continue
            if _scope_from_clause(clause) in ("headings", "table", "body"):
                continue
            if not any(style.scope == "all" for style in spec.text_styles):
                warnings.append("疑似字体/颜色作用域未覆盖全文")
                break

    if spec.duplicate_policy == "normal":
        match = DUPLICATE_HINT_PATTERN.search(text)
        if match:
            warnings.append(f"疑似重复约束未解析：{match.group(0)}")

    return list(dict.fromkeys(warnings))


def normalize_spec(raw: Any) -> Optional[NewDocSpec]:
    """把模型输出的 spec（对象/字典）规整为 NewDocSpec；非法返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, NewDocSpec):
        return raw
    try:
        return NewDocSpec.model_validate(raw)
    except Exception as exc:
        print(f"【写作约束】模型返回的 new_doc_spec 不合法，改用原话兜底：{exc}")
        return None


def _prefer_fallback(model_value, fallback_value, label: str):
    if model_value in (None, "", "unspecified"):
        return fallback_value
    if fallback_value in (None, "", "unspecified"):
        return model_value
    if str(model_value) != str(fallback_value):
        print(f"【约束冲突】{label} 模型={model_value} 原话={fallback_value}，采用原话值")
        return fallback_value
    return model_value


def _merge_title_style(model: Optional[TitleStyleSpec],
                       fallback: Optional[TitleStyleSpec]) -> Optional[TitleStyleSpec]:
    """总标题版式逐字段合并：原话写了用原话，原话没写用模型，都没有返回 None。"""
    if model is None:
        return fallback
    if fallback is None:
        return model
    return TitleStyleSpec(
        font_name=fallback.font_name or model.font_name,
        font_color=fallback.font_color or model.font_color,
        font_size=fallback.font_size if fallback.font_size is not None else model.font_size,
        bold=fallback.bold if fallback.bold is not None else model.bold,
        alignment=fallback.alignment or model.alignment,
        space_before=(
            fallback.space_before if fallback.space_before is not None else model.space_before
        ),
        space_after=(
            fallback.space_after if fallback.space_after is not None else model.space_after
        ),
    )


def merge_new_doc_spec(model_spec: Optional[NewDocSpec],
                       fallback_spec: Optional[NewDocSpec]) -> NewDocSpec:
    """模型与兜底合并：可直接核对的原话值优先；作用域样式按 scope 合并。"""
    fallback = fallback_spec or NewDocSpec()
    model = model_spec or NewDocSpec()
    max_chars = _prefer_fallback(model.max_body_chars, fallback.max_body_chars, "字数上限")
    counting_rule = (
        fallback.counting_rule if fallback.max_body_chars is not None
        and max_chars == fallback.max_body_chars else model.counting_rule
    )
    return NewDocSpec(
        title=_prefer_fallback(model.title, fallback.title, "标题"),
        title_style=_merge_title_style(model.title_style, fallback.title_style),
        max_body_chars=max_chars,
        counting_rule=counting_rule,
        # 口径是“怎么数”，以原话的显式说法为准：模型漏填时按 false，原话写明包含才为 true。
        count_include_title=bool(fallback.count_include_title or model.count_include_title),
        table_policy=_prefer_fallback(model.table_policy, fallback.table_policy, "表格策略"),
        text_styles=merge_text_styles(
            list(fallback.text_styles) + list(model.text_styles)
        ),
        required_topics=model.required_topics or fallback.required_topics,
        duplicate_policy=(
            model.duplicate_policy
            if model.duplicate_policy != "normal"
            else fallback.duplicate_policy
        ),
        document_form=_prefer_fallback(
            model.document_form, fallback.document_form, "文档形态"
        ),
        include_section_titles=_prefer_fallback(
            model.include_section_titles, fallback.include_section_titles, "章节标题要求"
        ),
        include_field_names=_prefer_fallback(
            model.include_field_names, fallback.include_field_names, "字段名称要求"
        ),
    )


def spec_from_state(state: Dict[str, Any], dialog_text: str) -> NewDocSpec:
    """从 state 读 spec；旧 checkpoint 没有 spec 时退化为原话解析。"""
    model_spec = normalize_spec((state or {}).get("new_doc_spec"))
    fallback_spec = parse_new_doc_spec(dialog_text)
    return merge_new_doc_spec(model_spec, fallback_spec)
