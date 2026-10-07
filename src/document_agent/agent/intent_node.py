from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple, Optional, Dict, Literal
from pydantic import BaseModel, Field

try:
    from document_agent.agent.agent_core import AgentState, get_last_user_text
    from document_agent.agent.llm_client import llm_model_invoke
except ImportError:
    from agent_core import AgentState, get_last_user_text
    from llm_client import llm_model_invoke

# 参考文件支持的后缀，与 extract_document 的解析能力保持一致
SUPPORTED_REFERENCE_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".json", ".csv", ".md"}



class RewriteStep(BaseModel):#复合改写指令中的一个执行步骤
    """复合改写指令的单个执行步骤：一条指令含多个诉求时，由大模型拆成有序的多个步骤。"""

    order: int = Field(default=1, description="执行顺序，从 1 开始，按 order 从小到大依次执行。")
    mode: Literal["replace", "patch", "global", "format"] = Field(
        description=(
            "本步骤使用的改写引擎：\n"
            "- 'replace': 全局查找替换（只换词，不改动文档结构）；\n"
            "- 'patch': 局部增删改（补充/修改/删除特定段落、条款、表格）；\n"
            "- 'global': 全局重塑与全文润色（整篇重写）；\n"
            "- 'format': 只规范排版（字体字号、标题层级、行距段距、对齐方式），文字一字不改。"
        )
    )
    target: Optional[str] = Field(
        default=None,
        description="本步骤的作用范围（如章节名'工作交接部分'、'全文'），不限定范围时留空。"
    )
    target_range: Optional[List[int]] = Field(
        default=None,
        description=(
            "本步骤作用范围的正文元素下标区间 [起始下标, 结束下标]（含两端）。"
            "仅当用户明确给出序号范围时才填，无法确定时留空，由改写节点按 target 在文档大纲中解析。"
        )
    )
    instruction: str = Field(
        default="",
        description="本步骤要完成的具体要求，只写与本步骤相关的内容，不要复述其它步骤的要求。"
    )
    replace_pairs: Dict[str, str] = Field(
        default_factory=dict,
        description="仅 mode 为 'replace' 时填写本步骤的 {旧词: 新词} 映射；其它模式必须为空字典。"
    )


class DocumentTaskIntent(BaseModel):
    """文档任务全量意图与实体槽位分析模型。
    
    用于从用户自由输入的自然语言指令和传入的文件池中，
    自动识别写作意图（改写还是新建）、挑选修改目标文档、区分参考材料、推导改写模式与提取另存为目标。
    """
    task_type: Literal["new", "rewrite"] = Field(
        description=(
            "任务类型：\n"
            "- 'rewrite': 用户希望对已有的某个 Word (.docx) 文档进行修改、改写、重写、局部修补、全文替换或公文润色；\n"
            "- 'new': 用户希望从零起草、全新撰写一份新文档（即使提供了参考资料，也是根据参考资料全新撰写）。"
        )
    )
    target_file: Optional[str] = Field(
        default=None,
        description=(
            "当 task_type 为 'rewrite' 时，从提供的候选文件列表中挑选出用户想要修改的目标文件名或文件路径。\n"
            "必须与候选文件列表中某一个完全对应（注意：必须是 .docx 格式文档）；若 task_type 为 'new' 则为 null。"
        )
    )
    rewrite_steps: List[RewriteStep] = Field(
        default_factory=list,
        description=(
            "改写指令的有序执行步骤清单——这是唯一决定执行哪些改写模式、以什么顺序执行的字段：\n"
            "1. 先判断指令是否为「复合指令」：只有当指令同时包含不同性质的诉求（改内容 / 换词 / 只排版）时才算复合；\n"
            "2. 非复合指令（单一诉求）：必须且只能给出恰好 1 个步骤，绝不能为了稳妥而多拆步骤；\n"
            "3. 复合指令：拆成 2~N 个步骤，order 从 1 递增，每步只描述自己那部分要求；\n"
            "   例如'把排版规范一下，并在工作交接部分再加一条电信诈骗提醒'是复合指令，拆为：\n"
            "   [{order:1, mode:'patch', target:'工作交接部分', instruction:'新增一条电信诈骗提醒'},"
            "{order:2, mode:'format', target:'全篇', instruction:'统一规范全文排版格式'}]；\n"
            "4. 顺序硬约束：replace 必须排最前；局部增补 patch 先于整篇重写 global；"
            "format 必须排最后（内容定稿后再统一排版，否则新增段落不会被规范化）；\n"
            "5. 严禁因为主诉求是格式规范就丢掉内容增补等次要诉求。"
        )
    )
    replace_pairs: Dict[str, str] = Field(
        default_factory=dict,
        description="当改写步骤中出现 replace 模式时，汇总提取所有要查找替换的词对 {原词: 新词}；非 replace 情况必须为空字典。"
    )
    save_mode: Literal["new_file", "overwrite"] = Field(
        default="new_file",
        description=(
            "保存模式：\n"
            "- 'overwrite': 用户明确要求原地覆盖修改原文件（例如指令包含'直接在原文件上改'、'覆盖原文件'）；\n"
            "- 'new_file': 另存为新文档（默认安全推荐。无论用户是否显式指定新文件名，"
            "为保障原始草稿数据安全，改写或新建文档默认均另存为新版/正式版/优化版文档）。"
        )
    )
    suggested_filename: Optional[str] = Field(
        default=None,
        description=(
            "建议的目标文件名（必须以 .docx 结尾）：\n"
            "1. 若用户在指令中明确指定了保存文件名（例如：'另存为 最终汇报.docx'），提取该文件名；\n"
            "2. 若用户未指定新文件名：\n"
            "   - 若为 new 模式（全新写作）：结合撰写主题智能拟定规范公文名称（如'关于XXX情况的报告.docx'）；\n"
            "   - 若为 rewrite 模式且为 new_file：根据原目标文件名、参考材料及改写目标，智能拟定一个专业得体的新文件名"
            "（例如：原文件是'11_粗糙排版_各业务线自报数据与零散问题汇总.docx'，参考了'正式版2'，可智能生成'11_各业务线自报数据与零散问题汇总_正式版.docx'或'11_各业务线自报数据与零散问题汇总_规范排版版.docx'）；\n"
            "3. 若 save_mode 为 'overwrite'，此处填原文件名或保持为 null。"
        )
    )
    save_filename: Optional[str] = Field(
        default=None,
        description="兼容旧字段，与 suggested_filename 含义一致。"
    )
    reasoning: str = Field(
        default="",
        description="做出该意图判断、文件挑选及文件名拟定的简要分析理由。"
    )


def resolve_rewrite_file(rewrite_path: str, input_files) -> tuple:
    """确定需要改写的文档路径，返回 (路径, 错误信息)。

    优先使用明确给出的路径；没有给出时，从用户提到的文件里挑选docx文档作为改写对象。
    用户提到的文件可能只是供检索用的参考资料（如pdf、txt），因此只考虑docx，
    并且只有候选唯一时才自动采用，避免改错文档。
    """
    rewrite_path = (rewrite_path or "").strip()
    if rewrite_path:
        return rewrite_path, ""
    docx_files = [str(path) for path in (input_files or []) if str(path).lower().endswith(".docx")]
    if len(docx_files) == 1:
        print("未指定改写文件，使用唯一的docx文件：", docx_files[0])
        return docx_files[0], ""
    if not docx_files:
        return "", "识别到改写意图，但没有可用的docx文档，请在需求里说明需要改写的文档路径"
    return "", f"识别到改写意图，但有多个docx文档：{docx_files}，请在需求里说明需要改写哪一个"


def _dedup_paths(files: List[str]) -> List[str]:
    #同一文档可能被重复登记，按绝对路径去重后再判断歧义
    return list(dict.fromkeys(str(Path(f).resolve()) for f in files))


def match_candidate_file(target_str: Optional[str], candidate_files: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """从候选文件列表中精准匹配目标文件。
    
    返回 (匹配到的实际绝对路径, 错误信息)。
    """
    if not target_str:
        return None, "未指明待修改的目标文档"

    target_clean = target_str.strip().strip("'\"")
    if not target_clean:
        return None, "目标文档名称为空"

    target_path = Path(target_clean)

    #显式给出非 .docx 后缀时直接拒绝：否则 foo.pdf 会在后续按主名/子串匹配到 foo.docx，改写错文档
    if target_path.suffix and target_path.suffix.lower() != ".docx":
        return None, f"改写目标必须是 .docx 文档：{target_clean}"

    # 1. 尝试直接按路径匹配（必须是 .docx）
    if target_path.exists() and target_path.suffix.lower() == ".docx":
        target_abs = str(target_path.resolve())
        for f in candidate_files:
            if str(Path(f).resolve()).lower() == target_abs.lower():
                return str(Path(f).resolve()), None
        # 路径存在但未登记在 candidate_files：拒绝，避免改写从未提供给系统的文档
        return None, f"目标文档不在候选文件列表中：{target_clean}"

    # 候选文件中的 docx（只有 docx 能作为改写目标）
    docx_candidates = [f for f in candidate_files if Path(f).suffix.lower() == ".docx"]
    if not docx_candidates:
        return None, "候选文件列表中不存在任何可作为改写目标的 .docx 文档"

    # 2. 按文件名完全匹配（不区分大小写）
    target_name_lower = target_path.name.lower()
    exact_matches = _dedup_paths([
        f for f in docx_candidates if Path(f).name.lower() == target_name_lower
    ])
    if len(exact_matches) == 1:
        return exact_matches[0], None
    if len(exact_matches) > 1:
        return None, f"候选文件中有 {len(exact_matches)} 个文档同名（{target_path.name}），请指明完整路径：" + "、".join(exact_matches)

    # 3. 按主文件名匹配
    target_stem_lower = target_path.stem.lower()
    stem_matches = _dedup_paths([
        f for f in docx_candidates if Path(f).stem.lower() == target_stem_lower
    ])
    if len(stem_matches) == 1:
        return stem_matches[0], None
    if len(stem_matches) > 1:
        return None, f"候选文件中有 {len(stem_matches)} 个文档主名相同（{target_path.stem}），请指明完整路径：" + "、".join(stem_matches)

    # 4. 子串模糊匹配
    substr_matches = _dedup_paths([
        f for f in docx_candidates
        if (target_clean.lower() in Path(f).name.lower() or Path(f).stem.lower() in target_clean.lower())
    ])
    if len(substr_matches) == 1:
        return substr_matches[0], None
    if len(substr_matches) > 1:
        return None, f"与 '{target_str}' 模糊匹配的文档有 {len(substr_matches)} 个，请指明具体文件名：" + "、".join(substr_matches)

    return None, f"在候选文件列表中未找到与 '{target_str}' 匹配的 .docx 文档"


def resolve_save_target(
    explicit_save_path: Optional[str],
    save_mode: str,
    suggested_filename: Optional[str],
    rewrite_file: Optional[str],
    user_intent: str = "new",
) -> str:
    """计算最终输出路径。
    
    优先级：
    1. 显式指定的 save_path 拥有最高优先级；
    2. 若用户明确要求 overwrite 原地覆盖，返回 rewrite_file 原绝对路径；
    3. 另存为新文件模式（save_mode == "new_file" 或未指定）：
       - 优先采用模型提取或智能推导的文件名 suggested_filename（严格过滤 null/none 等空值）；
       - 若未能推导出有效文件名：
         - 改写模式下，自动根据原文件名生成 '<原主文件名>_修改版.docx'，保存在与原文档同级目录下；
         - 新建模式下，自动生成 '新生成文档.docx'，保存在 data 目录下。
    """
    # 1. 显式 save_path 优先
    if explicit_save_path and explicit_save_path.strip():
        p = Path(explicit_save_path.strip())
        if p.suffix.lower() != ".docx":
            p = p.with_suffix(".docx")
        return str(p.resolve())

    # 2. 原地覆盖模式
    if save_mode == "overwrite" and rewrite_file:
        return str(Path(rewrite_file).resolve())

    # 3. 另存为新文件：清洗模型给出的文件名
    clean_name = ""
    candidate_name = (suggested_filename or "").strip().strip("'\"")
    # 严格过滤模型可能输出的字符串 null, none, nil, undefined 或空串
    stem = Path(candidate_name).stem.lower() if candidate_name else ""
    if candidate_name and stem not in ("null", "none", "nil", "undefined", ""):
        clean_name = Path(candidate_name).name
        if not clean_name.lower().endswith(".docx"):
            clean_name += ".docx"
        # 模型建议的文件名若与源文档同名，必须消歧，避免改写输出覆盖源文件
        if rewrite_file and Path(clean_name).name.lower() == Path(rewrite_file).name.lower():
            clean_name = f"{Path(rewrite_file).stem}_修改版.docx"

    # 4. 无有效文件名时兜底命名
    if not clean_name:
        if rewrite_file:
            clean_name = f"{Path(rewrite_file).stem}_修改版.docx"
        else:
            clean_name = "新生成文档.docx"

    # 5. 组装最终绝对路径
    if rewrite_file:
        target_dir = Path(rewrite_file).resolve().parent
        return str((target_dir / clean_name).resolve())
    else:
        data_dir = Path("data").resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return str((data_dir / clean_name).resolve())



def validate_reference_paths(reference_paths: Optional[List[str]]) -> Tuple[List[str], str]:
    """校验参考文件路径，返回 (有效路径列表, 错误信息)。

    逐一检查路径是否存在、后缀是否受支持；任一路径无效就返回错误信息，
    避免路径写错时被静默忽略。
    """
    valid = []
    for raw in reference_paths or []:
        path = str(raw).strip()
        if not path:
            continue
        path_obj = Path(path)
        if not path_obj.exists():
            return valid, f"指定的参考文件不存在：{path}，请确认路径后重试"
        if path_obj.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
            return valid, (f"指定的参考文件类型不支持：{path} "
                           f"（支持的类型：{'、'.join(sorted(SUPPORTED_REFERENCE_SUFFIXES))}）")
        valid.append(str(path_obj.resolve()))
    return valid, ""


def build_full_intent_prompt(user_prompt: str, candidate_files: List[str]) -> str:
    """构建用于全量意图与槽位分析的大模型提示词。"""
    if candidate_files:
        files_desc = "\n".join([f"- {Path(f).name} (完整路径: {f})" for f in candidate_files])
    else:
        files_desc = "(未提供任何候选文件)"

    return (
        "你是一个专业的智能文档写作与改写意图分析助手。用户提供了一组候选文件列表，并提出了一段自然语言指令。\n"
        "请根据用户指令和候选文件，深度理解用户的真实意图并提取关键槽位参数：\n\n"
        f"【候选文件列表】\n{files_desc}\n\n"
        f"【用户指令内容】\n{user_prompt}\n\n"
        "【分析与槽位抽取规范】\n"
        "1. task_type 判断：\n"
        "   - 'rewrite'：用户希望对已有的某份 Word (.docx) 文档进行修改、改写、局部调整、全文重构、替换或润色（例如：'把xxx里的第三条改为'、'根据A修改B'、'对xxx进行公文润色'）；\n"
        "   - 'new'：用户希望全新起草、生成一份新文档（例如：'参考资料写一份报告'、'起草一份合作协议'），不以修改某份已有文档为目标。\n"
        "2. target_file 挑选（仅当 task_type 为 rewrite 时）：\n"
        "   - 必须从候选文件列表中精准挑选出用户要修改的那份 Word (.docx) 文档；\n"
        "   - 注意：改写对象必须是 .docx 格式。非 .docx 文件（如 .pdf, .xlsx）只能作为参考资料，不能作为修改目标；\n"
        "   - 若候选列表有多个 docx，请仔细结合用户指令中的文件名、业务关键词进行匹配；\n"
        "   - 若 task_type 为 'new'，此处必须为 null。\n"
        "3. rewrite_steps 步骤编排（仅当 task_type 为 rewrite 时；这是唯一决定执行哪些改写模式与顺序的字段）：\n"
        "   -（a）先判断是否为「复合指令」：只有当指令同时包含不同性质的诉求（改内容 / 换词 / 只排版）时才算复合；\n"
        "   -（b）非复合指令（单一诉求）：必须且只能给出恰好 1 个步骤，绝不能为了稳妥而多拆步骤。例如：\n"
        "       * 只要求'格式规范/统一排版/统一字体字号/调整标题层级/统一行距段距/套用样式'且未要求改写、润色、重写、精简、扩写文字内容时，只给 1 步 mode='format'（严禁改成 'global'）；\n"
        "       * 只要求整篇风格重塑、公文规范化深度润色、逐章深度重写时，只给 1 步 mode='global'；\n"
        "       * 只要求修改/增删特定条款、段落微调、更新表格数据等局部操作时，只给 1 步 mode='patch'（最常用且保留原文档排版格式）；\n"
        "       * 只要求全文专有名词/术语/单位/年份等批量替换时，只给 1 步 mode='replace'，并在该步的 replace_pairs 中给出 {旧词: 新词}；\n"
        "   -（c）复合指令：拆成 2~N 个步骤，order 从 1 递增，每步的 mode/target/instruction 只描述本步要做的事；\n"
        "     例如'把这个文件的排版格式规范一下，生成一个新文件，并且在工作交接部分再多加一个电信诈骗提醒'，必须拆为：\n"
        "     [{order:1, mode:'patch', target:'工作交接部分', instruction:'在工作交接部分新增一条电信诈骗提醒'},"
        " {order:2, mode:'format', target:'全篇', instruction:'统一规范全文排版格式'}]\n"
        "   -（d）顺序硬约束：replace 排最前（纯词替换、不改变文档结构）；局部增补 patch 先于整篇重写 global；"
        "format 必须排最后（内容定稿后再统一排版，否则新增段落不会被规范化）；\n"
        "   -（e）若用户既要求改内容又要求规范格式，绝不能只给 format 而丢掉内容诉求。\n"
        "   -（f）target 只写范围的自然语言名称（章节标题或'全篇'）；target_range 仅在用户明确给出序号范围时"
        "填写 [起始下标, 结束下标]，无法确定时必须留空，交由改写节点按 target 在文档大纲中解析。\n"
        "4. replace_pairs 提取（仅当步骤中出现 replace 模式时）：\n"
        "   - 提取需要替换的词对字典，格式为 {原词: 替换后新词}，并与该 replace 步骤的 replace_pairs 保持一致；\n"
        "   - 非 replace 模式必须为空字典。\n"
        "5. save_mode 判断：\n"
        "   - 'overwrite'：用户在指令中明确提出直接修改原文件、覆盖原文档（例如：'直接在原文件上改'、'覆盖原文件'）；\n"
        "   - 'new_file'：另存为新文件（默认安全推荐。若用户未明确说明或指定了另存为文件名，为保障原稿安全均按 new_file 处理）。\n"
        "6. suggested_filename 提取或智能推导（必须以 .docx 结尾）：\n"
        "   - 若用户指令中明确指定了新文件名（如'另存为 汇报_v2.docx'、'保存到 输出.docx'），直接提取该文件名；\n"
        "   - 若用户未明确指定新文件名：\n"
        "     * 若为 new 模式：根据写作主题拟定规范公文名称（例如'关于XXX情况的报告.docx'）；\n"
        "     * 若为 rewrite 模式且为 new_file：根据待改写文件名、参考文件风格和改写意图智能拟定一个专业得体的新文件名"
        "（例如：原文件是'11_粗糙排版_各业务线自报数据与零散问题汇总.docx'，参考了'正式版2'，可智能拟定为'11_各业务线自报数据与零散问题汇总_正式版.docx'或'11_各业务线自报数据与零散问题汇总_规范排版版.docx'）；\n"
        "     * 若为 overwrite 模式：可填原文件名或保持为 null。\n"
        "7. reasoning：简要阐述判断依据、是否为复合指令及步骤拆分理由，以及文件名推导理由。"
    )


#步骤模式的展示名（与 rewrite_node 的调度打印保持一致）
STEP_MODE_LABELS = {
    "replace": "模式二：全局查找替换",
    "patch": "模式一：局部微调",
    "global": "模式三：全局重构与润色",
    "format": "模式四：格式规范化",
}


def build_rewrite_steps(task_intent, fallback_mode: str, replace_pairs: Dict[str, str]) -> List[Dict[str, Any]]:
    """把大模型给出的步骤清单规整为可执行的有序步骤，并打印编排结果。

    - 兼容缺省：模型未给 rewrite_steps 时退化为「单步骤 = fallback_mode」，行为与旧版一致；
    - 步骤内提取的 replace 词对会并入 state 的 replace_pairs；
    - 这里只做排序与合法性过滤，真正的依赖关系校正（replace 最前 / format 最后）由改写节点执行。
    """
    steps: List[Dict[str, Any]] = []
    for position, raw in enumerate(list(getattr(task_intent, "rewrite_steps", None) or [])):
        mode = str(getattr(raw, "mode", "") or "").strip().lower()
        if mode not in STEP_MODE_LABELS:
            continue
        try:
            order = int(getattr(raw, "order", 0) or position + 1)
        except (TypeError, ValueError):
            order = position + 1
        step_pairs = dict(getattr(raw, "replace_pairs", None) or {})
        if mode == "replace" and step_pairs:
            replace_pairs.update(step_pairs)
        steps.append({
            "order": order,
            "mode": mode,
            "target": str(getattr(raw, "target", "") or "").strip(),
            "target_range": getattr(raw, "target_range", None),
            "instruction": str(getattr(raw, "instruction", "") or "").strip(),
            "replace_pairs": step_pairs,
        })
    if not steps:
        steps = [{"order": 1, "mode": fallback_mode, "target": "", "target_range": None,
                  "instruction": "", "replace_pairs": {}}]
    steps.sort(key=lambda step: step["order"])
    for position, step in enumerate(steps, 1):
        step["order"] = position

    if len(steps) == 1:
        step = steps[0]
        scope = f"（范围：{step['target']}）" if step["target"] else ""
        print(f"【改写编排】单步骤：{STEP_MODE_LABELS[step['mode']]}{scope}")
    else:
        print(f"【改写编排】共 {len(steps)} 个步骤，将按序执行：")
        for step in steps:
            scope = f"范围：{step['target']}｜" if step["target"] else ""
            print(f"   步骤{step['order']}：{STEP_MODE_LABELS[step['mode']]}｜{scope}要求：{step['instruction'] or '（沿用整体要求）'}")
    return steps


def create_intent_node(llm=None):
    """创建初始化、参数校验与全量意图识别节点：意图、目标文件、有序步骤与另存文件名均由大模型从自然语言推断。"""
    def intent_node(state: AgentState) -> AgentState:
        raw_input_files = list(state.get("input_file_path") or [])
        explicit_save_path = (state.get("save_path") or "").strip()
        replace_pairs = dict(state.get("replace_pairs") or {})

        # 1. 基础校验输入文件是否存在与类型支持
        valid_input_files, ref_error = validate_reference_paths(raw_input_files)
        if ref_error:
            print(f"[参数校验错误] {ref_error}")
            return {
                **state,
                "state": "error",
                "error": ref_error,
            }

        user_prompt = get_last_user_text(state.get("messages")).strip()

        rewrite_file = None
        rewrite_mode = None
        rewrite_steps: List[Dict[str, Any]] = []
        extracted_save_filename = None
        user_intent = "new"

        # 2. 大模型全量分析指令与候选文件池
        if llm is not None and user_prompt:
            print("【意图分析】正在分析用户指令与候选文件...")
            intent_prompt = build_full_intent_prompt(user_prompt, valid_input_files)
            task_intent = llm_model_invoke(llm, intent_prompt, DocumentTaskIntent)

            if task_intent:
                print(f"【智能意图分析结果】任务类型: {task_intent.task_type.upper()} | 依据: {task_intent.reasoning}")
                if task_intent.task_type == "rewrite":
                    user_intent = "rewrite"
                    target_candidate = task_intent.target_file
                    matched_file, match_err = match_candidate_file(target_candidate, valid_input_files)

                    if match_err and target_candidate and Path(target_candidate.strip().strip("'\"")).suffix.lower() not in ("", ".docx"):
                        print(f"[意图识别错误] {match_err}")
                        return {**state, "user_intent": "rewrite", "state": "error", "error": match_err}

                    #模型没有给出明确路径时，从用户提到的文件里挑选docx文档，挑不到则直接报错
                    rewrite_file, error = resolve_rewrite_file(matched_file, valid_input_files)
                    if error:
                        return {**state, "user_intent": "rewrite", "rewrite_file": None, "state": "error", "error": error}

                    #顶层 replace_pairs 作为兜底：模型把词对写在步骤内时，build_rewrite_steps 会并入
                    if task_intent.replace_pairs:
                        replace_pairs.update(task_intent.replace_pairs)
                    #步骤清单是改写模式的唯一依据：非复合=恰好 1 步、复合=按序多步，由改写节点依次落地
                    rewrite_steps = build_rewrite_steps(task_intent, "patch", replace_pairs)
                    rewrite_mode = rewrite_steps[0]["mode"]  #兼容派生字段：等于第一步的模式
                    save_mode = task_intent.save_mode or "new_file"
                    extracted_save_filename = task_intent.suggested_filename or task_intent.save_filename
                else:
                    user_intent = "new"
                    rewrite_file = None
                    rewrite_mode = None
                    save_mode = task_intent.save_mode or "new_file"
                    extracted_save_filename = task_intent.suggested_filename or task_intent.save_filename
            else:
                # 大模型结构化解析失败时，默认按全新写作处理
                print("【意图分析】结构化解析无结果，按全新写作处理")
                user_intent = "new"
                rewrite_file = None
                rewrite_mode = None
                save_mode = "new_file"
        else:
            # 无大模型或无用户指令时的安全初始
            user_intent = "new"
            rewrite_file = None
            rewrite_mode = None
            save_mode = "new_file"

        # 3. 构建文件角色映射（保留全部文件，不剔除改写目标）
        file_roles: Dict[str, str] = {}
        for f in valid_input_files:
            f_abs = str(Path(f).resolve())
            if rewrite_file and f_abs.lower() == str(Path(rewrite_file).resolve()).lower():
                file_roles[f_abs] = "待修改目标文档"
            else:
                file_roles[f_abs] = "辅助参考资料/风格模板"

        # 4. 计算并解析最终输出路径
        final_save_path = resolve_save_target(
            explicit_save_path,
            save_mode,
            extracted_save_filename,
            rewrite_file,
            user_intent,
        )
        if final_save_path:
            if save_mode == "overwrite":
                print(f"【输出路径】原地覆盖原文件: {final_save_path}")
            else:
                print(f"【输出路径】另存为: {final_save_path}")
        else:
            print("【输出路径】自动保存至默认路径")

        if rewrite_file:
            print(f"【目标文档】{Path(rewrite_file).name}")
        ref_files = [
            f for f in valid_input_files
            if not rewrite_file or str(Path(f).resolve()).lower() != str(Path(rewrite_file).resolve()).lower()
        ]
        if ref_files:
            print(f"【参考资料】{len(ref_files)} 个文件: {[Path(f).name for f in ref_files]}")
        else:
            print("【参考资料】无额外辅助参考文件")

        return {
            **state,
            "user_intent": user_intent,
            "rewrite_file": rewrite_file,
            "rewrite_mode": rewrite_mode,
            "rewrite_steps": rewrite_steps,
            "replace_pairs": replace_pairs,
            "save_path": final_save_path,
            "input_file_path": valid_input_files,  # 保留全部文件
            "file_roles": file_roles,              # 各文件角色
        }

    return intent_node


def route_after_intent(state: AgentState) -> str:
    """初始化/校验出错时直接结束流程。"""
    if state.get("state") == "error":
        return "end"
    return "continue"
