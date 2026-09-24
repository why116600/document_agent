from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Literal
from pydantic import BaseModel, Field

try:
    from document_agent.agent.agent_core import AgentState, get_last_user_text
    from document_agent.agent.llm_client import llm_model_invoke
except ImportError:
    from agent_core import AgentState, get_last_user_text
    from llm_client import llm_model_invoke

# 参考文件支持的后缀，与 extract_document 的解析能力保持一致
SUPPORTED_REFERENCE_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".json", ".csv", ".md"}


class RewriteIntentAnalysis(BaseModel):
    """改写意图分析结果模型（兼容旧版调用）。"""
    mode: str = Field(
        description=(
            "改写模式，必须是以下三者之一：\n"
            "- 'replace'：全局查找替换。用于全文批量替换特定专有名词、公司名称、人名、术语、年份、单位等明确的对应词；\n"
            "- 'patch'：局部微调修补。用于修改、增加或删除特定条款、章节、段落或表格，不需要重构整篇文档；\n"
            "- 'global'：全局重塑与全文润色。用于对整篇文档进行语言风格重构、公文规范化深度润色、全文结构重构。"
        )
    )
    replace_pairs: Dict[str, str] = Field(
        default_factory=dict,
        description="当 mode 为 'replace' 时，提取用户要求替换的所有词对映射（原词为 key，替换后新词为 value）；非 replace 模式必须为空字典。"
    )
    reason: str = Field(default="", description="做出该模式选择的分析判断理由")


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
    rewrite_mode: Optional[Literal["patch", "replace", "global"]] = Field(
        default="patch",
        description=(
            "当 task_type 为 'rewrite' 时的改写策略：\n"
            "- 'replace': 全局查找替换。全文批量替换特定专有名词、人名、公司名、术语、年份等；\n"
            "- 'patch': 局部微调修补。针对文档的特定条款、段落、章节、表格增删改；\n"
            "- 'global': 全局重塑与全文润色。对整篇文档语言风格规范化深度润色、全文重构。"
        )
    )
    replace_pairs: Dict[str, str] = Field(
        default_factory=dict,
        description="当 rewrite_mode 为 'replace' 时，提取所有要查找替换的词对 {原词: 新词}；非 replace 模式必须为空字典。"
    )
    save_filename: Optional[str] = Field(
        default=None,
        description=(
            "用户在指令中是否明确要求另存为特定文件名或路径（例如：'另存为 最终版.docx'、'保存到 输出.docx'）。\n"
            "若用户未明确指定另存为（例如只说修改、未提及新文件名），必须填 null。"
        )
    )
    reasoning: str = Field(
        default="",
        description="做出该意图判断和文件挑选的简要分析理由。"
    )


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

    # 1. 尝试直接按路径匹配（必须是 .docx）
    if target_path.exists() and target_path.suffix.lower() == ".docx":
        target_abs = str(target_path.resolve())
        for f in candidate_files:
            if str(Path(f).resolve()).lower() == target_abs.lower():
                return str(Path(f).resolve()), None
        # 如果路径存在且是 .docx，即便未显式登记在 candidate_files 也接受
        return str(target_path.resolve()), None

    # 过滤出所有 candidate 中的 docx 文件（只有 docx 允许作为改写目标）
    docx_candidates = [f for f in candidate_files if Path(f).suffix.lower() == ".docx"]
    if not docx_candidates:
        return None, "候选文件列表中不存在任何可作为改写目标的 .docx 文档"

    # 2. 按文件名（含后缀）完全匹配（不区分大小写，且仅限 docx）
    target_name_lower = target_path.name.lower()
    exact_matches = [
        str(Path(f).resolve())
        for f in docx_candidates
        if Path(f).name.lower() == target_name_lower
    ]
    if len(exact_matches) >= 1:
        return exact_matches[0], None

    # 3. 按主文件名（不含后缀）匹配 .docx 文件
    target_stem_lower = target_path.stem.lower()
    stem_matches = [
        str(Path(f).resolve())
        for f in docx_candidates
        if Path(f).stem.lower() == target_stem_lower
    ]
    if len(stem_matches) >= 1:
        return stem_matches[0], None

    # 4. 子串包含模糊匹配（仅限 .docx）
    substr_matches = [
        str(Path(f).resolve())
        for f in docx_candidates
        if (target_clean.lower() in Path(f).name.lower() or Path(f).stem.lower() in target_clean.lower())
    ]
    if len(substr_matches) >= 1:
        return substr_matches[0], None

    return None, f"在候选文件列表中未找到与 '{target_str}' 匹配的 .docx 文档"


def resolve_save_target(
    explicit_save_path: Optional[str],
    extracted_filename: Optional[str],
    rewrite_file: Optional[str],
) -> str:
    """计算最终输出路径。
    
    优先级：
    1. 显式指定的 save_path 拥有最高优先级；
    2. 指令中提取出的另存为文件名 save_filename：
       - 若为绝对路径且以 .docx 结尾直接采用；
       - 若为纯文件名且为改写模式，保存在与改写目标文档同级目录下；
       - 若为纯文件名且为新建模式，保存在 data 目录下；
    3. 若均未指定：
       - 改写模式下返回空字符串（后续 rewrite_node 默认原地覆盖）；
       - 新建模式下返回空字符串（后续 docx_node 默认生成新文档路径）。
    """
    if explicit_save_path and explicit_save_path.strip():
        p = Path(explicit_save_path.strip())
        if p.suffix.lower() != ".docx":
            p = p.with_suffix(".docx")
        return str(p.resolve())

    if extracted_filename and extracted_filename.strip():
        raw_name = extracted_filename.strip().strip("'\"")
        # 移除非法字符，提取文件名
        clean_name = re.sub(r'[\\/:*?"<>|]', '_', Path(raw_name).name)
        if not clean_name.lower().endswith(".docx"):
            clean_name += ".docx"

        if rewrite_file:
            target_dir = Path(rewrite_file).resolve().parent
            return str((target_dir / clean_name).resolve())
        else:
            data_dir = Path("data").resolve()
            data_dir.mkdir(parents=True, exist_ok=True)
            return str((data_dir / clean_name).resolve())

    return ""


def resolve_rewrite_file(rewrite_path: Optional[str], input_files: List[str]) -> Tuple[str, str]:
    """确定需要改写的文档路径，返回 (路径, 错误信息)。

    优先使用明确给出的路径；没有给出时，从输入文件列表里挑选唯一的 docx 文档作为改写对象。
    """
    rewrite_path = (rewrite_path or "").strip()
    if rewrite_path:
        p = Path(rewrite_path)
        if not p.exists():
            return "", f"指定的改写文档不存在：{rewrite_path}"
        if p.suffix.lower() != ".docx":
            return "", f"改写目标只支持 .docx 格式：{rewrite_path}"
        return str(p.resolve()), ""

    docx_files = [str(Path(path).resolve()) for path in (input_files or []) if str(path).lower().endswith(".docx")]
    if len(docx_files) == 1:
        print("未显式指定改写文件，自动采用唯一的 docx 文件：", docx_files[0])
        return docx_files[0], ""
    if not docx_files:
        return "", "已进入改写模式，但未指定需要改写的 docx 文档路径"
    return "", f"已进入改写模式，但检测到多个 docx 文档：{docx_files}，请明确指定需要改写哪一个"


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
        "3. rewrite_mode 判断（仅当 task_type 为 rewrite 时）：\n"
        "   - 'replace'：全文专有名词/术语/单位/年份等批量查找替换。必须在 replace_pairs 中提取出对应的 {旧词: 新词}；\n"
        "   - 'global'：整篇文档行文风格全面重塑、公文规范化深度润色、全文逐章深度重写；\n"
        "   - 'patch'：修改/增删特定条款、段落微调、更新表格数据等局部操作（最常用且保留原文档排版格式）。\n"
        "4. replace_pairs 提取：\n"
        "   - 仅在 replace 模式下提取需要替换的词对字典，格式为 {原词: 替换后新词}；其它模式必须为空字典。\n"
        "5. save_filename 提取：\n"
        "   - 如果用户在指令中明确指定了另存为的新文件名或新路径（如'另存为 最终版.docx'、'保存到 输出.docx'、'输出为新版.docx'），提取该文件名；\n"
        "   - 如果用户未指定另存为（只是原地修改或未提及新文件名），必须为 null。\n"
        "6. reasoning：简要阐述判断依据。"
    )


def create_intent_node(llm=None):
    """创建初始化、参数校验与全功能意图与实体槽位智能识别节点。

    具备以下核心能力：
    1. 【自然语言意图全自动识别】：无需依赖命令行显式指定模式，大模型自主从指令中推断是全新写作还是改写；
    2. 【候选文件池智能分流】：从传入的一堆文件中，自动区分哪个是待修改的目标文档，哪些是辅助参考资料；
    3. 【改写策略三层细分】：精准分类为 patch（局部修补）、replace（全局替换）或 global（全局重构润色）；
    4. 【另存为目标解析】：自动从用户指令中提取另存为文件名并拼装目标路径；
    5. 【显式参数优先与向下兼容】：若调用方显式提供了 rewrite_file 或明确的 rewrite_mode，优先遵循显式配置。
    """
    def intent_node(state: AgentState) -> AgentState:
        raw_input_files = list(state.get("input_file_path") or [])
        rewrite_file = (state.get("rewrite_file") or "").strip()
        rewrite_mode = (state.get("rewrite_mode") or "auto").strip().lower()
        replace_pairs = dict(state.get("replace_pairs") or {})
        explicit_save_path = (state.get("save_path") or "").strip()

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
        docx_candidates = [f for f in valid_input_files if Path(f).suffix.lower() == ".docx"]

        # 2. 判断是否需要大模型执行全量意图与实体抽取
        # 条件：未显式指定 rewrite_file，且未显式指定 user_intent，且有传入文件和 prompt
        need_llm_task_analysis = (
            not rewrite_file
            and state.get("user_intent") is None
            and llm is not None
            and bool(user_prompt)
        )

        extracted_save_filename = None

        if need_llm_task_analysis:
            print("【意图分析】未显式指定改写文件，正在通过大模型智能分析用户指令与候选文件...")
            intent_prompt = build_full_intent_prompt(user_prompt, valid_input_files)
            task_intent = llm_model_invoke(llm, intent_prompt, DocumentTaskIntent)

            if task_intent:
                print(f"【智能意图分析结果】任务类型: {task_intent.task_type.upper()} | 依据: {task_intent.reasoning}")
                if task_intent.task_type == "rewrite":
                    user_intent = "rewrite"
                    target_candidate = task_intent.target_file
                    matched_file, match_err = match_candidate_file(target_candidate, valid_input_files)

                    if not matched_file:
                        # 兜底尝试：如果候选文件中刚好只有一个 .docx 文件，自动采用
                        if len(docx_candidates) == 1:
                            matched_file = docx_candidates[0]
                            print(f"【智能匹配兜底】未精准匹配到名称，自动采用候选列表中唯一的 docx 文档: {matched_file}")
                        elif len(docx_candidates) == 0:
                            err_msg = (
                                "识别到您的需求为修改已有文档，但在提供的候选文件列表中未找到任何可修改的 Word (.docx) 文档。\n"
                                f"（当前候选文件：{valid_input_files}）"
                            )
                            print(f"[意图识别错误] {err_msg}")
                            return {**state, "user_intent": "rewrite", "state": "error", "error": err_msg}
                        else:
                            err_msg = (
                                f"识别到改写需求，但候选列表中存在多个 docx 文档 ({docx_candidates})，"
                                f"未能明确确定要修改哪一个（模型识别目标: '{target_candidate}'）。请在指令中指明具体要修改的文件名。"
                            )
                            print(f"[意图识别错误] {err_msg}")
                            return {**state, "user_intent": "rewrite", "state": "error", "error": err_msg}

                    rewrite_file = matched_file
                    rewrite_mode = task_intent.rewrite_mode or "patch"
                    if rewrite_mode == "replace" and task_intent.replace_pairs:
                        replace_pairs.update(task_intent.replace_pairs)
                    extracted_save_filename = task_intent.save_filename
                else:
                    user_intent = "new"
                    rewrite_file = None
                    rewrite_mode = None
                    extracted_save_filename = task_intent.save_filename
            else:
                # 大模型结构化解析失败时的鲁棒兜底
                print("【意图分析】大模型结构化解析未返回有效结果，启用规则兜底...")
                # 简单规则启发式：指令包含改写关键词且有单一 docx
                rewrite_keywords = ["改写", "修改", "替换", "更新", "重写", "润色", "补充", "删减", "增删"]
                has_rewrite_kw = any(kw in user_prompt for kw in rewrite_keywords)
                if has_rewrite_kw and len(docx_candidates) == 1:
                    user_intent = "rewrite"
                    rewrite_file = docx_candidates[0]
                    rewrite_mode = "patch"
                    print(f"【规则兜底】判定为改写文档: {rewrite_file} (模式: patch)")
                else:
                    user_intent = "new"
                    rewrite_file = None
                    rewrite_mode = None
                    print("【规则兜底】判定为全新写作")
        else:
            # 3. 显式指定模式或无须全量推断（向下兼容）
            is_rewrite = bool(rewrite_file or (state.get("user_intent") == "rewrite"))
            if is_rewrite:
                user_intent = "rewrite"
                resolved_rewrite, error = resolve_rewrite_file(rewrite_file, valid_input_files)
                if error:
                    print(f"[参数校验错误] {error}")
                    return {**state, "user_intent": "rewrite", "rewrite_file": None, "state": "error", "error": error}
                rewrite_file = resolved_rewrite

                # 若改写模式仍为 auto，利用大模型进一步确定三层改写模式与提取另存为目标
                if rewrite_mode in ["auto", "", None] and not replace_pairs and llm is not None:
                    print(f"【工作模式】改写文档: {rewrite_file} (模式：auto，正在智能推断改写策略...)")
                    intent_prompt = build_full_intent_prompt(user_prompt, [rewrite_file] + valid_input_files)
                    analysis = llm_model_invoke(llm, intent_prompt, DocumentTaskIntent)
                    if analysis and analysis.rewrite_mode in ["replace", "patch", "global"]:
                        rewrite_mode = analysis.rewrite_mode
                        if rewrite_mode == "replace" and analysis.replace_pairs:
                            replace_pairs.update(analysis.replace_pairs)
                        extracted_save_filename = analysis.save_filename
                        print(f"【智能意图推断】成功推断为 -> {rewrite_mode.upper()} 模式（依据: {analysis.reasoning}）")
                    else:
                        rewrite_mode = "patch"
                        print("【智能意图推断】推断未返回明确模式，安全兜底采用 -> PATCH 模式")
                elif rewrite_mode == "auto":
                    rewrite_mode = "replace" if replace_pairs else "patch"
                    print(f"【工作模式】改写文档: {rewrite_file} (自动采用 {rewrite_mode} 模式)")
                else:
                    print(f"【工作模式】改写文档: {rewrite_file} (显式指定模式: {rewrite_mode})")
            else:
                user_intent = "new"
                rewrite_file = None
                rewrite_mode = None
                print("【工作模式】全新写作")

        # 4. 参考文件剥离：如果改写目标也被包含在输入文件中，将其剥离，避免自引与重复解析
        if rewrite_file:
            ref_files = [f for f in valid_input_files if Path(f).resolve() != Path(rewrite_file).resolve()]
        else:
            ref_files = valid_input_files

        # 5. 计算并解析最终输出路径
        final_save_path = resolve_save_target(explicit_save_path, extracted_save_filename, rewrite_file)
        if final_save_path:
            print(f"【输出路径】解析确定文档保存路径为: {final_save_path}")
        else:
            if user_intent == "rewrite":
                print(f"【输出路径】未指定另存为路径，默认将原地保存覆盖原文档: {rewrite_file}")
            else:
                print("【输出路径】未显式指定，将在生成后自动保存至默认路径")

        if ref_files:
            print(f"【参考资料】已确认 {len(ref_files)} 个辅助参考文件: {[Path(f).name for f in ref_files]}")
        else:
            print("【参考资料】无辅助参考文件")

        return {
            **state,
            "user_intent": user_intent,
            "rewrite_file": rewrite_file,
            "rewrite_mode": rewrite_mode,
            "replace_pairs": replace_pairs,
            "save_path": final_save_path,
            "input_file_path": ref_files,
        }

    return intent_node


def route_after_intent(state: AgentState) -> str:
    """初始化/校验出错时直接结束流程。"""
    if state.get("state") == "error":
        return "end"
    return "continue"
