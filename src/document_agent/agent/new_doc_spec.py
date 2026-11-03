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


class NewDocSpec(BaseModel):
    title: Optional[str] = Field(
        default=None, description="用户明确指定的总标题；未指定为 null，不得自行推断。"
    )
    max_body_chars: Optional[int] = Field(
        default=None, description="正文长度上限；未指定为 null。"
    )
    counting_rule: CountingRule = Field(
        default="non_whitespace",
        description="字数口径：non_whitespace=非空白字符，chinese_chars=汉字，all_chars=全部字符。",
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


def parse_new_doc_spec(text: str) -> NewDocSpec:
    """确定性兜底：覆盖常见表达；未明确要求的字段保持 null/unspecified。"""
    max_body_chars, counting_rule = extract_body_limit(text)
    return NewDocSpec(
        title=extract_doc_title(text),
        max_body_chars=max_body_chars,
        counting_rule=counting_rule,
        table_policy=extract_table_policy(text),
        text_styles=extract_text_styles(text),
        required_topics=extract_required_topics(text),
        duplicate_policy=extract_duplicate_policy(text),
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
        max_body_chars=max_chars,
        counting_rule=counting_rule,
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
    )


def spec_from_state(state: Dict[str, Any], dialog_text: str) -> NewDocSpec:
    """从 state 读 spec；旧 checkpoint 没有 spec 时退化为原话解析。"""
    model_spec = normalize_spec((state or {}).get("new_doc_spec"))
    fallback_spec = parse_new_doc_spec(dialog_text)
    return merge_new_doc_spec(model_spec, fallback_spec)
