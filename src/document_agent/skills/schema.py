"""文档 Skill 的声明式数据结构与校验规则。

Skill 是一份可持久化的配置（JSON/YAML），只描述「怎么写、查什么、引什么、怎么验收」，
不包含可执行代码：加载时由 pydantic 做结构与取值校验，渲染为提示词后交给现有节点执行。
Skill 包目录中的 SKILL.md、references/、assets/ 由 loader 读取后填充到运行时字段。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Skill id 只允许小写字母、数字、下划线与短横线，便于作为目录名与命令行参数
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# 当前系统实际具备的检索来源；技能若声明其它来源只能退化或报错，不能凭空造出连接器
AVAILABLE_SOURCES = ("file", "knowledge")


def explicit_source_restriction(text: str) -> Optional[List[str]]:
    """识别明确的单来源指令；复杂或未识别的自然语言仍需用户核对。"""
    text = re.sub(r"\s+", "", text or "")
    prefix = r"(?<!不)(?:仅|只)(?:允许|能|可)?(?:使用|采用|依据|根据|检索)?"
    file_only = re.search(prefix + r"(?:用户(?:提供|上传)的?|提供的?|上传的?|本地的?|参考的?)?(?:文件|附件|资料)", text)
    knowledge_only = re.search(prefix + r"(?:组织|本地|远程)?(?:知识库|知识图谱)", text)
    if file_only and knowledge_only:
        raise ValueError("同时声明仅文件和仅知识库，来源要求互相冲突")
    return ["file"] if file_only else ["knowledge"] if knowledge_only else None

# 验收检查项；terminology 为兼容旧配置保留，由 verify_node 的语义评估处理，不做程序化检查
VERIFICATION_CHECKS = (
    "required_sections",
    "section_order",
    "must_include",
    "required_data",
    "citations",
    "citation_sources",
    "citation_numbers",
    "unsupported_claims",
    "terminology",
    "banned_expressions",
    "no_placeholder",
    "min_length",
    "format",
)

# 默认执行强度：程序可判定项按硬约束，受口径影响或需人工确认的项按软约束
DEFAULT_ENFORCEMENT = {
    "required_sections": "hard",
    "section_order": "hard",
    "must_include": "soft",
    "required_data": "hard",
    "citations": "hard",
    "citation_sources": "hard",
    "citation_numbers": "soft",
    "unsupported_claims": "soft",
    "banned_expressions": "hard",
    "no_placeholder": "hard",
    "min_length": "soft",
    "format": "soft",
    "terminology": "advisory",
}

# 变更摘要里使用的字段中文标签；新增字段时只在此处补充
SKILL_FIELD_LABELS = {
    "name": "名称", "description": "适用场景", "enabled": "启用状态", "created_by": "作者",
    "task": "写作要求", "structure": "文档结构", "research": "检索要求", "evidence": "证据要求",
    "verification": "验收要求", "instructions": "执行说明", "template": "模板选择",
    "recommendations": "可选建议",
}

MAX_SECTIONS = 40
MAX_INSTRUCTIONS_CHARS = 8000
MAX_REFERENCE_ITEMS = 20
MAX_REFERENCE_CHARS = 6000


class SkillValidationError(ValueError):
    """Skill 配置不合法。"""


class _StrictModel(BaseModel):
    # 禁止未知字段：用户写错键名时在加载阶段就报错，而不是被静默忽略
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LengthTarget(_StrictModel):
    min_chars: Optional[int] = Field(default=None, ge=0, le=200000, description="正文最少字符数")
    max_chars: Optional[int] = Field(default=None, ge=0, le=200000, description="正文最多字符数")

    @model_validator(mode="after")
    def _check_range(self):
        if self.min_chars is not None and self.max_chars is not None and self.min_chars > self.max_chars:
            raise ValueError("task.length_target.min_chars 不能大于 max_chars")
        return self


class TaskSpec(_StrictModel):
    doc_type: Literal["new", "rewrite", "any"] = Field(
        default="any", description="适用任务：new 全新写作、rewrite 改写、any 均适用"
    )
    triggers: List[str] = Field(
        default_factory=list,
        max_length=20,
        description="任务触发词：用户原话出现这些词时优先匹配本 Skill",
    )
    purpose: str = Field(default="", max_length=500, description="文档用途")
    audience: str = Field(default="", max_length=300, description="目标读者")
    tone: str = Field(default="", max_length=300, description="写作风格与语气")
    professionalism: str = Field(default="", max_length=100, description="专业程度，如「专业严谨」「通俗易懂」")
    language: str = Field(default="zh-CN", max_length=30, description="输出语言")
    length_target: LengthTarget = Field(default_factory=LengthTarget)
    terminology: List[str] = Field(
        default_factory=list, max_length=30, description="必须统一写法的术语（含推荐译名/缩写）"
    )
    banned_expressions: List[str] = Field(
        default_factory=list, max_length=30, description="禁用表达（如口语化、夸大、模糊措辞）"
    )
    extra_requirements: List[str] = Field(
        default_factory=list, max_length=20, description="其它写作约束（每条一句话）"
    )

    @field_validator("triggers", "terminology", "banned_expressions")
    @classmethod
    def _check_short_items(cls, value: List[str]) -> List[str]:
        for item in value:
            if len(item) > 60:
                raise ValueError("task 下的触发词/术语/禁用表达单项不得超过 60 字")
        return value

    @field_validator("extra_requirements")
    @classmethod
    def _check_extra(cls, value: List[str]) -> List[str]:
        for item in value:
            if len(item) > 200:
                raise ValueError("task.extra_requirements 单条不得超过 200 字")
        return value


class SectionSpec(_StrictModel):
    key: str = Field(default="", max_length=64, description="章节标识（可选，便于定位）")
    title: str = Field(min_length=1, max_length=100, description="章节标题，生成时必须原样出现")
    level: int = Field(default=1, ge=1, le=4, description="Word 大纲级别：1 为章，2 为节")
    required: bool = Field(default=True, description="是否为必填章节")
    must_include: List[str] = Field(
        default_factory=list, max_length=20, description="按字面检查的必含关键词；自然语言内容要求放 requirements"
    )
    requirements: List[str] = Field(default_factory=list, max_length=20, description="本章语义要求，由模型结合原文片段审核")
    data_fields: List[str] = Field(default_factory=list, max_length=30, description="本章必须呈现的 research.data_fields 字段")
    min_body_chars: int = Field(default=1, ge=1, le=20000, description="不含标题的正文最少字符数，拦截空章")
    evidence_types: List[str] = Field(
        default_factory=list, max_length=5, description="本章需要的证据类型，如「市场规模数据」「政策原文」"
    )
    evidence_required: bool = Field(default=False, description="本章是否必须带来源引用")
    min_citations: int = Field(default=0, ge=0, le=10, description="本章最少引用条数")
    guidance: str = Field(default="", max_length=500, description="本章写作要求")

    @field_validator("must_include", "evidence_types")
    @classmethod
    def _check_keywords(cls, value: List[str]) -> List[str]:
        for item in value:
            if len(item) > 60:
                raise ValueError("structure.sections[].must_include/evidence_types 单项不得超过 60 字")
        return value


class FormatSpec(_StrictModel):
    body_font: str = Field(default="", max_length=40, description="正文字体名，如「宋体」")
    body_size: Optional[float] = Field(default=None, ge=8, le=72, description="正文字号（磅）")
    heading_font: str = Field(default="", max_length=40, description="标题字体名，如「黑体」")
    heading_sizes: Dict[int, float] = Field(
        default_factory=dict, description="各级标题字号，键为大纲级别 1~4"
    )
    heading_bold: Optional[bool] = Field(default=None, description="标题是否加粗")
    line_spacing: Optional[float] = Field(default=None, ge=0.5, le=5, description="行距倍数")
    first_line_indent: Optional[float] = Field(default=None, ge=0, le=200, description="首行缩进（磅）")

    @field_validator("heading_sizes")
    @classmethod
    def _check_heading_sizes(cls, value: Dict[int, float]) -> Dict[int, float]:
        for level, size in value.items():
            if not 1 <= int(level) <= 4:
                raise ValueError("format.heading_sizes 的键只能是 1~4")
            if not 8 <= float(size) <= 72:
                raise ValueError("format.heading_sizes 的值必须在 8~72 磅之间")
        return {int(level): float(size) for level, size in value.items()}


class StructureSpec(_StrictModel):
    order: Literal["strict", "flexible"] = Field(
        default="flexible", description="章节顺序是否强制；flexible 时 sections 可为空"
    )
    numbering: str = Field(default="", max_length=60, description="章节编号体例，如「一、」「1.」")
    output_format: str = Field(default="", max_length=300, description="整体输出格式要求（段落/表格/图）")
    format: FormatSpec = Field(default_factory=FormatSpec, description="字体字号与段落格式")
    sections: List[SectionSpec] = Field(
        default_factory=list, max_length=MAX_SECTIONS, description="章节清单，按期望顺序排列"
    )

    @model_validator(mode="after")
    def _check_sections(self):
        if self.order == "strict" and not self.sections:
            raise ValueError("structure.order 为 strict 时必须给出 structure.sections")
        titles = [section.title for section in self.sections]
        if len(titles) != len(set(titles)):
            raise ValueError("structure.sections 中存在重复的章节标题")
        keys = [section.key for section in self.sections if section.key]
        if len(keys) != len(set(keys)):
            raise ValueError("structure.sections 中存在重复的章节 key")
        return self


class SourcePolicy(_StrictModel):
    priority: List[Literal["file", "knowledge"]] = Field(
        default_factory=lambda: ["file", "knowledge"],
        min_length=1,
        description="允许使用的检索来源及优先级；当前仅支持 file（用户提供的文件）与 knowledge（知识库）",
    )

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, value: List[str]) -> List[str]:
        if len(value) != len(set(value)):
            raise ValueError("research.source.priority 中存在重复来源")
        return value


class KnowledgeRetrievalSpec(_StrictModel):
    """知识库检索参数，直接作用于 gdb_retrieve_node 的向量检索与图扩展。"""

    top_k: int = Field(default=10, ge=1, le=50, description="向量检索召回节点数")
    min_similarity: float = Field(default=0.0, ge=0, le=1, description="最低余弦相似度，低于该值的结果被丢弃")
    expand_neighbors: bool = Field(default=True, description="是否允许沿摘要树向下展开邻居节点")
    max_expansions: int = Field(default=10, ge=1, le=50, description="邻居展开的最大轮次")


class FileRetrievalSpec(_StrictModel):
    """文件检索参数：候选文件上限、分块粒度与单文件字符上限。"""

    max_chars: int = Field(
        default=0, ge=0, le=200000,
        description="单个文件送入模型的最大字符数；0 表示按分块策略自动控制",
    )
    max_files_per_attempt: int = Field(
        default=0, ge=0, le=50,
        description="一次逻辑尝试最多检索几个候选文件；0 表示全部候选，超出时按相关度取前 N 并记录未检索文件",
    )
    chunk_chars: int = Field(
        default=1200, ge=200, le=6000, description="分块检索的单块目标字符数"
    )
    top_k_chunks: int = Field(
        default=6, ge=1, le=30, description="每次检索送入模型的相关块数量上限"
    )


class ResearchSpec(_StrictModel):
    questions: List[str] = Field(
        default_factory=list, max_length=20, description="需要在来源中回答的研究问题"
    )
    data_fields: List[str] = Field(
        default_factory=list, max_length=30, description="必须收集的数据字段/指标"
    )
    numeric_fields: List[str] = Field(
        default_factory=list, max_length=30, description="data_fields 中必须出现数值的字段；非数值口径仍需语义审核"
    )
    field_aliases: Dict[str, List[str]] = Field(default_factory=dict, max_length=30, description="数据字段的等价写法；键必须属于 data_fields")
    time_range: str = Field(
        default="", max_length=100, description="检索时间范围（知识库无时间字段，仅作为提示词与写作依据）"
    )
    region: str = Field(default="", max_length=100, description="地域范围（同为提示词约束）")
    max_rounds: int = Field(
        default=3, ge=1, le=10,
        description="逻辑检索尝试总预算（跨问题共享）；一次尝试可覆盖多个候选文件",
    )
    max_tool_calls: int = Field(
        default=12, ge=1, le=100,
        description="底层检索工具调用上限：文件按个计，知识库按次计",
    )
    max_total_context_chars: int = Field(
        default=60000, ge=1000, le=500000, description="检索素材送入模型的总字符预算"
    )
    per_question_attempts: int = Field(
        default=2, ge=1, le=5, description="单个研究问题最多尝试几次逻辑检索（跨查询式），用尽后记为缺失"
    )
    source: SourcePolicy = Field(default_factory=SourcePolicy)
    knowledge: KnowledgeRetrievalSpec = Field(default_factory=KnowledgeRetrievalSpec)
    file: FileRetrievalSpec = Field(default_factory=FileRetrievalSpec)
    query_expansion: bool = Field(default=True, description="结果不足时是否允许改写 query 继续检索")
    on_empty: Literal["rewrite_query", "fallback_source", "mark_missing"] = Field(
        default="rewrite_query", description="检索为空时的处理方式"
    )
    stop_when: str = Field(default="", max_length=200, description="提前结束检索的条件")
    stop_after_no_new_evidence: int = Field(
        default=0, ge=0, le=10, description="连续几轮没有新增证据时停止，0 表示关闭；程序直接执行"
    )
    credibility_rule: str = Field(
        default="", max_length=300, description="来源可信度与时效性要求（提示词约束，无法自动判定）"
    )

    @field_validator("questions")
    @classmethod
    def _check_questions(cls, value: List[str]) -> List[str]:
        for item in value:
            if not item.strip() or len(item) > 200:
                raise ValueError("research.questions 单条不得为空或超过 200 字")
        return value

    @field_validator("data_fields")
    @classmethod
    def _check_fields(cls, value: List[str]) -> List[str]:
        for item in value:
            if not item.strip() or len(item) > 60:
                raise ValueError("research.data_fields 单项不得为空或超过 60 字")
        return value

    @model_validator(mode="after")
    def _check_budget(self):
        if not set(self.numeric_fields).issubset(self.data_fields):
            raise ValueError("research.numeric_fields 必须是 research.data_fields 的子集")
        if not set(self.field_aliases).issubset(self.data_fields):
            raise ValueError("research.field_aliases 的键必须属于 research.data_fields")
        for aliases in self.field_aliases.values():
            if len(aliases) > 10 or any(not a.strip() or len(a) > 80 for a in aliases):
                raise ValueError("每个字段最多10个别名，别名不得为空或超过80字")
        question_count = len(self.questions) or (1 if self.data_fields else 0)
        if self.max_rounds < question_count:
            raise ValueError(
                f"research.max_rounds={self.max_rounds} 小于研究问题数 {question_count}，"
                "每个问题至少需要一次检索；请增加预算或合并问题"
            )
        return self


class EvidenceSpec(_StrictModel):
    derived_data: Literal["label", "forbid"] = Field(default="label", description="label 允许依据已有数据计算并明确标注，forbid 禁止补算缺失指标")
    citation_template: str = Field(
        default="【来源：{name}】",
        max_length=80,
        description="正文引用标记模板，必须含 {name} 占位符",
    )
    min_sources_per_key_claim: int = Field(
        default=1, ge=0, le=10, description="关键结论最少来源数（提示词约束，验收按章节引用数检查）"
    )
    cross_validation: bool = Field(default=False, description="是否要求交叉验证")
    conflict_policy: str = Field(default="", max_length=300, description="来源冲突时的处理方式")

    @field_validator("citation_template")
    @classmethod
    def _check_template(cls, value: str) -> str:
        if value and "{name}" not in value:
            raise ValueError("evidence.citation_template 必须包含 {name} 占位符")
        return value


class VerificationSpec(_StrictModel):
    checks: List[
        Literal[
            "required_sections",
            "section_order",
            "must_include",
            "required_data",
            "citations",
            "citation_sources",
            "citation_numbers",
            "unsupported_claims",
            "terminology",
            "banned_expressions",
            "no_placeholder",
            "min_length",
            "format",
        ]
    ] = Field(
        default_factory=lambda: [
            "required_sections",
            "section_order",
            "citations",
            "no_placeholder",
            "min_length",
        ],
        description="生成后需要执行的验收检查项",
    )
    enforcement: Dict[str, Literal["hard", "soft", "advisory"]] = Field(
        default_factory=dict,
        description="覆盖单个验收项的执行强度：hard 失败即不通过，soft 未确认记为未完成，advisory 仅提示",
    )
    semantic_review: bool = Field(
        default=True, description="是否在程序化检查后调用模型做语义补充评估（术语口径、交叉验证、数据一致性等）"
    )
    max_section_attempts: int = Field(default=3, ge=1, le=3, description="每章生成及受控修复的总次数上限")
    on_failure: Literal["report"] = Field(
        default="report", description="验收未通过时的处理方式；当前仅支持 report（只报告不阻断）"
    )

    @field_validator("checks")
    @classmethod
    def _check_checks(cls, value: List[str]) -> List[str]:
        if len(value) != len(set(value)):
            raise ValueError("verification.checks 中存在重复检查项")
        return value

    @field_validator("enforcement")
    @classmethod
    def _check_enforcement(cls, value: Dict[str, str]) -> Dict[str, str]:
        unknown = sorted(set(value) - set(VERIFICATION_CHECKS))
        if unknown:
            raise ValueError("verification.enforcement 只能覆盖已支持的验收项：" + "、".join(unknown))
        return value

    def strength(self, name: str) -> str:
        """返回某个验收项的执行强度；未显式配置时取内置默认值。"""
        return self.enforcement.get(name) or DEFAULT_ENFORCEMENT.get(name, "hard")


class ReferenceDoc(_StrictModel):
    """references/ 目录下的单个参考文档（运行时由 loader 读取）。"""

    name: str = Field(min_length=1, max_length=200, description="文件相对路径")
    content: str = Field(default="", max_length=MAX_REFERENCE_CHARS, description="文档内容")


class DocumentSkill(_StrictModel):
    id: str = Field(min_length=1, max_length=64, description="Skill 唯一标识，同时作为目录名")
    name: str = Field(min_length=1, max_length=80, description="展示名称")
    version: int = Field(default=1, ge=1, le=99, description="版本号")
    description: str = Field(default="", max_length=500, description="适用场景说明")
    enabled: bool = Field(default=True, description="是否启用；禁用后不会被列出或自动匹配")
    created_by: str = Field(default="", max_length=100, description="创建者")
    created_at: Optional[datetime] = Field(default=None, description="创建时间（ISO-8601）")
    updated_at: Optional[datetime] = Field(default=None, description="更新时间（ISO-8601）")
    task: TaskSpec = Field(default_factory=TaskSpec)
    structure: StructureSpec = Field(default_factory=StructureSpec)
    research: ResearchSpec = Field(default_factory=ResearchSpec)
    evidence: EvidenceSpec = Field(default_factory=EvidenceSpec)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    recommendations: List[str] = Field(
        default_factory=list, max_length=30,
        description="用户与规范未明确指定时采用的推荐默认值，包括模型补充的篇幅、指标等；不作为硬性验收条件",
    )
    # 以下字段由 loader 从 SKILL.md / references/ / assets/ 填充，也可在单文件配置中直接给出
    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARS, description="SKILL.md 正文指令")
    references: List[ReferenceDoc] = Field(
        default_factory=list, max_length=MAX_REFERENCE_ITEMS, description="references/ 下的参考文档"
    )
    template: str = Field(default="", max_length=200, description="assets/ 下模板的相对路径（可选）")
    template_path: str = Field(default="", max_length=1000, description="模板的运行时绝对路径")

    @field_validator("recommendations")
    @classmethod
    def _check_recommendations(cls, value: List[str]) -> List[str]:
        if any(not item.strip() or len(item) > 300 for item in value):
            raise ValueError("recommendations 每条建议不得为空或超过300字")
        return value

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not SKILL_ID_PATTERN.match(value):
            raise ValueError("id 只能包含小写字母、数字、下划线与短横线，且以字母或数字开头（不超过 64 字符）")
        return value

    @model_validator(mode="after")
    def _check_consistency(self):
        for section in self.structure.sections:
            if not set(section.data_fields).issubset(self.research.data_fields):
                raise ValueError(f"章节 {section.title} 的 data_fields 必须属于 research.data_fields")
        restriction = explicit_source_restriction(self.research.credibility_rule + "\n" + self.instructions)
        if restriction and self.research.source.priority != restriction:
            raise ValueError(f"明确的单来源指令要求 {restriction}，但 research.source.priority={self.research.source.priority}")
        citation_checks = {"citations", "citation_sources"} & set(self.verification.checks)
        if citation_checks and "{name}" not in self.evidence.citation_template:
            raise ValueError(
                "verification.checks 包含 citations/citation_sources 时，"
                "evidence.citation_template 必须包含 {name} 占位符"
            )
        return self

    def to_state_dict(self) -> Dict[str, Any]:
        """序列化为可放入 AgentState 的纯字典（供 LangGraph 状态合并与检查点保存）。"""
        return self.model_dump(mode="json", exclude_none=True)

    def to_config_dict(self) -> Dict[str, Any]:
        """只导出配置字段，排除由目录内容派生的运行时字段。"""
        return self.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"references", "template_path"},
        )

    def summary(self) -> str:
        status = "启用" if self.enabled else "禁用"
        return (
            f"{self.name}（{self.id} v{self.version}，{status}）｜适用：{self.task.doc_type}｜"
            f"章节：{len(self.structure.sections)}｜检索轮次：{self.research.max_rounds}｜"
            f"来源：{'/'.join(self.research.source.priority)}"
        )


def coerce_skill(value: Any) -> Optional[DocumentSkill]:
    """把状态里的 Skill 字典还原为模型；已经是对应实例或为空时原样返回。"""
    if value is None:
        return None
    if isinstance(value, DocumentSkill):
        return value
    if isinstance(value, dict):
        try:
            return DocumentSkill.model_validate(value)
        except ValidationError as exc:
            print(f"[Skill] 状态里的规范配置不合法，本次不启用规范：{exc}")
            return None
    return None
