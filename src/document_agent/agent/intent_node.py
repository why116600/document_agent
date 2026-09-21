from pathlib import Path
from typing import List, Tuple, Optional, Dict
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
    """改写意图分析结果模型。"""
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


def resolve_rewrite_file(rewrite_path: Optional[str], input_files: List[str]) -> Tuple[str, str]:
    """确定需要改写的文档路径，返回 (路径, 错误信息)。

    优先使用明确给出的路径；没有给出时，从输入文件列表里挑选唯一的docx文档作为改写对象。
    """
    rewrite_path = (rewrite_path or "").strip()
    if rewrite_path:
        p = Path(rewrite_path)
        if not p.exists():
            return "", f"指定的改写文档不存在：{rewrite_path}"
        if p.suffix.lower() != ".docx":
            return "", f"改写目标只支持 .docx 格式：{rewrite_path}"
        return str(p), ""

    docx_files = [str(path) for path in (input_files or []) if str(path).lower().endswith(".docx")]
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
        valid.append(str(path_obj))
    return valid, ""


def create_intent_node(llm=None):
    """创建初始化、参数校验与改写意图智能识别节点。

    如果用户未显式传入 -m（或为 auto），利用大模型自然语言识别自动分类为
    replace（全局替换）、patch（局部修补）或 global（全局重塑润色）。
    """
    def intent_node(state: AgentState) -> AgentState:
        input_files = list(state.get("input_file_path") or [])
        rewrite_file = (state.get("rewrite_file") or "").strip()
        rewrite_mode = (state.get("rewrite_mode") or "auto").strip().lower()
        replace_pairs = dict(state.get("replace_pairs") or {})

        # 单一真相来源判定：提供改写目标或显式要求改写时进入 rewrite 分支，其余为 new
        is_rewrite = bool(rewrite_file or (state.get("user_intent") == "rewrite"))

        if is_rewrite:
            user_intent = "rewrite"
            resolved_rewrite, error = resolve_rewrite_file(rewrite_file, input_files)
            if error:
                print(f"参数校验错误: {error}")
                return {**state, "user_intent": "rewrite", "rewrite_file": None, "state": "error", "error": error}
            rewrite_file = resolved_rewrite
            # 如果改写文档也包含在参考文件列表中，将其排除，避免自引与重复解析
            input_files = [f for f in input_files if Path(f).resolve() != Path(rewrite_file).resolve()]

            # 智能意图推断：当用户未显式指定模式（即 auto），或者没有预设替换词对时，由 LLM 分析
            if rewrite_mode in ["auto", "", None] and not replace_pairs and llm is not None:
                user_prompt = get_last_user_text(state.get("messages"))
                print(f"【工作模式】改写文档: {rewrite_file} (模式：auto，正在智能推断改写意图...)")
                intent_prompt = (
                    "你是一个文档改写意图分析助手。用户希望对已有的 Word 文档进行改写。\n"
                    "请根据用户的改写要求，精准判断应该采取哪种改写策略：\n"
                    "1. replace（全局查找替换）：用户希望全文批量替换某些专有名词、公司名称、年份、术语等。\n"
                    "   例如：'把全文的“甲方”改成“委托方”'、'把2023年改成2026年'。\n"
                    "   如果是此类需求，请在 replace_pairs 中提取出所有要替换的键值对。\n"
                    "2. global（全局重塑润色）：用户希望对整篇文档进行语言风格重构、公文规范化深度润色、全文逐章深度重写。\n"
                    "   例如：'把全文润色为严谨的企业公文规范'、'重新整理整篇文档的行文逻辑'。\n"
                    "3. patch（局部微调修补）：用户希望修改/增删特定条款、段落、考核指标、更新表格，或者意图较为具体或局部。\n"
                    "   例如：'把第三章的考核指标改为每月一次'、'在第二节后增加一条保密条款'。\n"
                    "   注意：只要不是全篇术语替换或全文彻底重塑，优先选择 patch（最安全且保留原有排版格式）。\n\n"
                    f"用户需求内容：\n{user_prompt}\n"
                )
                analysis = llm_model_invoke(llm, intent_prompt, RewriteIntentAnalysis)
                if analysis and analysis.mode in ["replace", "patch", "global"]:
                    rewrite_mode = analysis.mode
                    if rewrite_mode == "replace" and analysis.replace_pairs:
                        replace_pairs.update(analysis.replace_pairs)
                    print(f"【智能意图推断】成功推断为 -> {rewrite_mode.upper()} 模式（依据: {analysis.reason}）")
                    if replace_pairs:
                        print(f"【智能意图推断】提取到替换词对: {replace_pairs}")
                else:
                    rewrite_mode = "patch"
                    print("【智能意图推断】推断未返回明确模式，安全兜底采用 -> PATCH 模式（局部微调修补）")
            elif rewrite_mode == "auto":
                # 没有 LLM 实例或者已有替换对时的兜底
                rewrite_mode = "replace" if replace_pairs else "patch"
                print(f"【工作模式】改写文档: {rewrite_file} (自动采用 {rewrite_mode} 模式)")
            else:
                print(f"【工作模式】改写文档: {rewrite_file} (显式指定模式: {rewrite_mode})")
        else:
            user_intent = "new"
            rewrite_file = None
            rewrite_mode = None
            print("【工作模式】全新写作")

        valid_refs, ref_error = validate_reference_paths(input_files)
        if ref_error:
            print(f"参数校验错误: {ref_error}")
            return {
                **state,
                "user_intent": user_intent,
                "rewrite_file": rewrite_file,
                "rewrite_mode": rewrite_mode,
                "replace_pairs": replace_pairs,
                "state": "error",
                "error": ref_error,
            }

        if valid_refs:
            print(f"【参考文件】加载了 {len(valid_refs)} 个有效参考文件: {valid_refs}")
        else:
            print("【参考文件】无参考文件")

        return {
            **state,
            "user_intent": user_intent,
            "rewrite_file": rewrite_file,
            "rewrite_mode": rewrite_mode,
            "replace_pairs": replace_pairs,
            "input_file_path": valid_refs,
        }
    return intent_node


def route_after_intent(state: AgentState) -> str:
    """初始化/校验出错时直接结束流程。"""
    if state.get("state") == "error":
        return "end"
    return "continue"
