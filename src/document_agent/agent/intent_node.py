from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

from llm_client import llm_model_invoke
from agent_core import AgentState

#参考文件支持的后缀，与 extract_document 的解析能力保持一致
SUPPORTED_REFERENCE_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".json", ".csv", ".md"}


class WriteIntent(BaseModel):#用户的写作意图
    intent: str = Field(description="用户意图，new表示全新写作，rewrite表示改写已有文档，output表示把已有内容整理成文档")
    rewrite_path: str = Field(default="", description="需要改写的文档路径，如果用户不是要改写文档则输出空字符串")
    reference_paths: List[str] = Field(default_factory=list, description="用户要求作为参考内容的文件路径列表，没有则为空列表")
    save_path: str = Field(default="", description="用户指定的输出文档保存路径，如果用户没有指定则输出空字符串")


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


def validate_reference_paths(reference_paths) -> tuple:
    """校验用户指定的参考文件路径，返回 (有效路径列表, 错误信息)。

    逐一检查路径是否存在、后缀是否受支持；任一路径无效就返回错误信息，
    避免路径写错时被静默忽略，导致在缺少用户指定资料的情况下继续生成文档。
    """
    valid = []
    for raw in reference_paths or []:
        path = str(raw).strip()
        if not path:
            continue
        path_obj = Path(path)
        if not path_obj.exists():
            return valid, f"用户指定的参考文件不存在：{path}，请确认路径后重试"
        if path_obj.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
            return valid, (f"用户指定的参考文件类型不支持：{path}"
                           f"（支持的类型：{'、'.join(sorted(SUPPORTED_REFERENCE_SUFFIXES))}）")
        valid.append(path)
    return valid, ""


def create_intent_node(llm):
    def intent_node(state: AgentState) -> AgentState:
        input_files = state.get("input_file_path") or []
        #调用方已经明确了写作类型时，直接使用，不再调用模型判断
        preset_intent = state.get("user_intent")
        if preset_intent in ("new", "rewrite"):
            if preset_intent == "new":
                print("使用指定的写作类型：new")
                return {**state}
            rewrite_file, error = resolve_rewrite_file(state.get("rewrite_file"), input_files)
            if error:
                return {**state, "state": "error", "error": error}
            print("使用指定的写作类型：rewrite 改写文件：", rewrite_file)
            return {**state, "rewrite_file": rewrite_file}
        messages = state["messages"]
        file_list = "\n".join(input_files) if input_files else "无"
        prompt = (
            "你是一个企业文档写作系统的意图识别助手，需要根据用户对话判断用户的写作意图。\n"
            "意图分为三种：\n"
            "new：用户要求新写、重新生成或从头写一份文档；\n"
            "rewrite：用户要求对已有的文档进行改写、修订、扩写、精简、换风格、更新内容等；\n"
            "output：用户要求把已有的内容整理输出成一份文档。\n"
            f"用户对话内容：{messages}\n"
            f"调用方已经提供的文件：{file_list}\n"
            "请判断用户意图，并且：\n"
            "- 如果用户要改写某个文档，把该文档的完整路径填入rewrite_path，否则留空；\n"
            "- 如果用户要求参考某些文件（例如：参考 D:/a.pdf 写一份总结），把这些文件的完整路径填入reference_paths，没有则为空列表；\n"
            "- 如果用户指定了输出文档的保存路径，填入save_path，否则留空。\n"
            "路径必须是用户在对话中给出的原始路径，不要自己编造。\n"
        )
        intent = llm_model_invoke(llm, prompt, WriteIntent)
        if intent is None:#兜底，宁可退化成可以解释的默认行为也不会崩溃
            print("识别用户写作意图失败，默认按新写文档处理")
            return {**state, "user_intent": "new", "rewrite_file": None}
        rewrite_path = (intent.rewrite_path or "").strip()
        if intent.intent == "rewrite":
            #模型没有给出明确路径时，从用户提到的文件里挑选docx文档，挑不到则直接报错
            rewrite_path, error = resolve_rewrite_file(rewrite_path, input_files)
            if error:
                return {**state, "user_intent": "rewrite", "rewrite_file": None, "state": "error", "error": error}
        reference_paths, reference_error = validate_reference_paths(intent.reference_paths)
        if reference_error:
            print(reference_error)
            return {**state, "user_intent": intent.intent, "rewrite_file": rewrite_path or None,
                    "state": "error", "error": reference_error}
        print("识别用户意图：", intent.intent, "改写文件：", rewrite_path,
              "参考文件：", reference_paths, "保存路径：", intent.save_path)
        return {
            **state,
            "user_intent": intent.intent,
            "rewrite_file": rewrite_path or None,
            "save_path": (state.get("save_path") or "").strip() or (intent.save_path or "").strip(),
            "input_file_path": reference_paths or (state.get("input_file_path") or []),
        }
    return intent_node


def route_after_intent(state: AgentState) -> str:
    #意图识别出错时直接结束流程。
    if state.get("state") == "error":
        return "end"
    return "continue"

