from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict, Tuple, Annotated
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from llm_client import get_deepseek_llm

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    state: str
    retrieve_tool: str #要使用的检索工具
    retrieve_target: str#检索目标
    retrieve_params: Optional[Dict[str,str]]
    retrieve_count_limit: int#检索的限制次数
    file_retrived_items: Optional[Dict[Tuple,str]]#检索文档、关键词到检索结果的映射
    retrieved_content: List[str]# 检索出来的内容
    input_file_path: List[str]#用户提供的作为参考内容的文件路径
    file_summaries: Dict[str, str]#文件路径到文件摘要的映射
    gdb_label: str#要查询的图数据库的节点标签
    user_intent: Optional[str]#用户写作意图，new表示全新写作，rewrite表示改写
    rewrite_file: Optional[str]#待改写的文档路径
    rewrite_mode: Optional[str]#改写动作：replace（确定性换词）、edit（局部增删改）、rewrite（整段/整章重写）、format（仅格式规范化）；有步骤清单时等于第一步的 action
    rewrite_steps: List[Dict[str, Any]]#有序改写步骤清单：复合指令由大模型拆成多步，每步含 action/scope/instruction/replace_pairs（兼容旧 mode/target），改写节点逐步重建快照后依次执行
    replace_pairs: Optional[Dict[str, str]]#replace 模式下旧词到新词的映射
    save_path: str#输出文档的保存路径，为空时自动生成
    new_doc_spec: Optional[Dict[str, Any]]#新建文档的结构化约束（标题/字数/表格/字体作用域等），可选，旧 checkpoint 无此字段
    output_dir: Optional[str]#输出目录：为空时新建文档保存到 data 目录，Web 版传入会话文件夹
    document_citations: List[Dict[str, Any]]#远程知识图谱检索到的可引用文档：[{id, full_name, title, path}]，full_name 为完整文件名
    file_roles: Optional[Dict[str, str]]#文件角色标签映射（如待修改目标文档、辅助参考/风格模板）
    error: Optional[str]#错误结果
    skill_id: Optional[str]#命令行显式指定的 Skill id 或文件路径
    skill_name: Optional[str]#意图模型根据用户需求选择的 Skill id 或名称
    skill: Optional[Dict[str, Any]]#已校验的 Skill 配置（DocumentSkill 的纯字典形式）
    evidence_records: List[Dict[str, Any]]#检索证据的来源与片段，供引用与验收使用
    verification_report: Optional[Dict[str, Any]]#落盘后的验收报告
    capabilities: Optional[Dict[str, Any]]#本次运行中来源/模板/权限的可用性预检结果
    retrieval_plan: Optional[Dict[str, Any]]#计划驱动的检索队列（问题、来源、查询、预算）
    retrieval_progress: Optional[Dict[str, Any]]#检索进度（已覆盖/缺失问题、尝试记录）
    retrieval_report: Optional[Dict[str, Any]]#检索完成后的覆盖报告
    citations: List[Dict[str, Any]]#整理后的可引用来源清单
    unmet_requirements: List[str]#最终未满足的检索/验收要求
    generation_report: Optional[List[Dict[str, Any]]]#各章的生成尝试和检查失败记录
    use_skill: Optional[bool]
    excluded_skills: List[str]
    skill_reason: str
    intent_prepared: bool
    skill_target: Optional[str]
    result_type: Optional[str]
    skill_operation: Optional[Dict[str, Any]]
    message: Optional[str]


def get_last_user_text(messages) -> str:
    """取出最后一条用户消息的文本，兼容消息对象与字典两种形式。"""
    for message in reversed(list(messages or [])):
        if isinstance(message, dict):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        elif getattr(message, "type", "") == "human":
            return str(getattr(message, "content", "") or "")
    return ""

def route_intent(state: AgentState) -> str:#检索完成后根据用户意图选择改写已有文档还是从零编写文档
    if state.get("user_intent") == "rewrite" and state.get("rewrite_file"):
        return "rewrite"
    return "new"

def route_retrieve(state: AgentState) -> str:
    tool = str(state.get("retrieve_tool", "end")).strip().lower()
    if tool == "file":
        return "file"
    elif tool == "knowledge":
        return "knowledge"
    # 当检索完成或没有有效工具调用时，按用户意图流转至改写或新建节点
    if tool == "end" and not (state.get("retrieved_content") or []) and (state.get("file_summaries") or {}):
        # 有摘要却没有任何检索要点：写作节点将走摘要兜底
        print("【资料检查】检索结束时无要点，写作阶段使用参考资料摘要兜底")
    return route_intent(state)

class AgentCore:
    def __init__(self):
        self.llm=get_deepseek_llm()
        self.graph=None
        self.agent_invoke=None
        self.checkpointer=InMemorySaver()#使用检查点记录会话内容
        #知识图谱检索阶段的模式：由 setting.json 的 remote_kg.enabled 决定
        self.knowledge_mode="local"#local=本地图数据库，remote=远程知识图谱
        self.remote_kg_settings=None#RemoteKGSettings 快照
        self.remote_kg_tool=None#远程模式下复用的 RemoteKGTool 实例

    def _build_remote_kg_tool(self, settings):
        """按配置创建远程知识图谱客户端（整个会话复用同一实例）。"""
        try:
            from document_agent.retrieve_tool.remote_kg_tool import RemoteKGTool
        except ImportError:#兼容只把 agent 目录加入 sys.path 的启动方式
            from remote_kg_tool import RemoteKGTool
        return RemoteKGTool(settings.base_url, **settings.tool_kwargs())

    def build_graph(self, gdb, kg=None, remote_kg=None, *, knowledge_required=True): #构建agent图
        """组装检索与写作节点。

        :param gdb: 本地 Memgraph 篇章树；远程知识图谱模式下不使用，可传 None
        :param kg: 本地知识图谱记忆；远程知识图谱模式下不使用，可传 None
        :param remote_kg: 远程知识图谱配置（RemoteKGSettings）；缺省时读 setting.json，
            其 enabled 决定知识图谱检索节点用远程还是本地
        """
        from remote_kg_config import RemoteKGSettings, load_remote_kg_settings
        from intent_node import create_intent_node, route_after_intent
        from summary_node import create_summary_node
        from retrieve_node import create_retrieve_node
        from docx_node import create_new_docx_node
        from rewrite_node import create_rewrite_node
        from file_retrieve_node import create_file_retrieve_node
        from skill_node import create_skill_node, route_after_skill
        from verify_node import create_verify_node

        settings = remote_kg if isinstance(remote_kg, RemoteKGSettings) else load_remote_kg_settings()
        self.remote_kg_settings = settings
        self.knowledge_mode = settings.mode if knowledge_required else "files"
        print(f"[知识图谱] {settings.describe()}")

        self.graph = StateGraph(AgentState)
        self.graph.add_node("intent", create_intent_node(self.llm))
        self.graph.add_node("summary", create_summary_node(self.llm))
        self.graph.add_node("retrieve", create_retrieve_node(self.llm))
        self.graph.add_node("retrieve_file", create_file_retrieve_node(self.llm))
        #知识图谱检索节点：按配置在“远程知识图谱”与“本地图数据库”之间二选一
        use_remote = settings.enabled and knowledge_required
        if not knowledge_required:
            self.remote_kg_tool = None
            knowledge_node = "retrieve_disabled"
            self.graph.add_node(knowledge_node, lambda state: {
                **state, "state": "error", "error": "本次仅文件模式禁止知识库检索"
            })
        elif use_remote:
            from kg_retrieve_node import create_kg_retrieve_node
            self.remote_kg_tool = self._build_remote_kg_tool(settings)
            knowledge_node = "retrieve_remote_kg"
            self.graph.add_node(
                knowledge_node, create_kg_retrieve_node(self.llm, self.remote_kg_tool)
            )
        else:
            from gdb_retrieve_node import create_gdb_retrieve_node
            self.remote_kg_tool = None
            knowledge_node = "retrieve_gdb"
            self.graph.add_node(knowledge_node, create_gdb_retrieve_node(self.llm, gdb, kg))
        self.graph.add_node(
            "skill", create_skill_node(self.llm, gdb, kg, remote_kg_tool=self.remote_kg_tool)
        )
        self.graph.add_node("verify", create_verify_node(self.llm))
        self.graph.add_node('manage_skill', self.manage_skill)
        #两个写作节点要按同一模式渲染检索资料（远程结果带来源标记）
        self.graph.add_node("new_docx", create_new_docx_node(self.llm, knowledge_mode=self.knowledge_mode))
        self.graph.add_node("rewrite_docx", create_rewrite_node(self.llm, knowledge_mode=self.knowledge_mode))
        self.graph.set_entry_point("intent")
        #意图识别出错时直接结束流程
        self.graph.add_conditional_edges(
            "intent",
            route_after_intent,
            {"continue":"skill","manage_skill":"manage_skill","end":END},
        )
        self.graph.add_edge('manage_skill', END)
        #Skill 不存在时原样放行，结构与未启用 Skill 时保持一致
        self.graph.add_conditional_edges(
            "skill",
            route_after_skill,
            {"continue":"summary","end":END},
        )
        self.graph.add_edge("summary","retrieve")
        self.graph.add_edge("retrieve_file","retrieve")
        if use_remote or not knowledge_required:
            self.graph.add_conditional_edges(
                knowledge_node,
                lambda state: "end" if state.get("state") == "error" else "continue",
                {"end": END, "continue": "retrieve"},
            )
        else:
            self.graph.add_edge(knowledge_node,"retrieve")
        #按检索工具与用户意图决定流转（继续检索 / 改写 / 新建 / 结束）
        self.graph.add_conditional_edges(
            "retrieve",
            route_retrieve,
            {"rewrite":"rewrite_docx","new":"new_docx","file":"retrieve_file","knowledge":knowledge_node,"end":END},
        )
        #生成完成后统一进入验收：未配置 Skill 时直接放行，保持原有行为
        self.graph.add_edge("new_docx","verify")
        self.graph.add_edge("rewrite_docx","verify")
        self.graph.add_edge("verify",END)
        self.agent_invoke=self.graph.compile(checkpointer=self.checkpointer)
        
    @staticmethod
    def _initial_state(user_input, input_file_path=None, save_path='', output_dir=None, skill_id=None):
        return {
            "messages": [{"role": "user", "content": user_input}],
            "retrieve_target": "",
            "state": "start",
            "input_file_path": list(input_file_path or []),
            "file_summaries": {},
            "gdb_label": "global",
            "retrieved_content": [],
            "retrieve_count_limit": 3,
            "retrieve_tool": "end",
            "retrieve_params": {},
            "file_retrived_items": {},
            "user_intent": None,
            "rewrite_file": None,
            "rewrite_mode": None,
            "rewrite_steps": [],
            "replace_pairs": {},
            "save_path": save_path or "",
            "output_dir": output_dir or "",
            "document_citations": [],
            "file_roles": {},
            "error": None,
            "skill_id": skill_id,
            "skill_name": None,
            "skill": None,
            "evidence_records": [],
            "verification_report": None,
            "capabilities": None,
            "retrieval_plan": None,
            "retrieval_progress": None,
            "retrieval_report": None,
            "citations": [],
            "unmet_requirements": [],
            "generation_report": None,
            'use_skill': None,
            'excluded_skills': [],
            'skill_reason': '',
            'intent_prepared': False,
            'skill_target': None,
            'result_type': None,
            'skill_operation': None,
            'message': None,
        }

    def prepare_task(self, user_input, input_file_path=None, *, save_path='', output_dir=None, skill_id=None):
        """只分析任务与选择规范，不连接知识库；结果供图执行时直接复用。"""
        from intent_node import create_intent_node
        state = self._initial_state(user_input, input_file_path, save_path, output_dir, skill_id)
        return create_intent_node(self.llm)(state)

    def manage_skill(self, state):
        """控制台与图节点共用的规则管理入口，不检索或生成文档。"""
        from document_agent.skills.commands import manage_skill_request
        try:
            operation = manage_skill_request(get_last_user_text(state.get('messages')), state.get('user_intent'),
                                             target=state.get('skill_target'), llm=self.llm)
            return {**state, 'state': 'succeeded', 'result_type': 'skill', 'skill_operation': operation,
                    'message': operation['message'], 'save_path': '', 'error': None}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return {**state, 'state': 'error', 'result_type': 'skill', 'error': str(exc)}

    def invoke(self, user_input: str, input_file_path: Optional[List[str]] = None,
               session_id: str = "default_session", save_path: str = "", output_dir: Optional[str] = None,
               skill_id: Optional[str] = None, *, prepared_state: Optional[AgentState] = None) -> AgentState:
        """执行任务；入口预先分析的结果直接复用，避免再次调用意图模型。"""
        if self.agent_invoke is None:
            raise ValueError("未初始化agent的图")
        state = prepared_state if prepared_state is not None else self._initial_state(user_input, input_file_path, save_path, output_dir, skill_id)
        if get_last_user_text(state.get('messages')) != user_input:
            raise ValueError('预分析结果不属于当前用户需求')
        if prepared_state is not None and {
            Path(p).resolve() for p in input_file_path or []
        } != {Path(p).resolve() for p in state.get('input_file_path') or []}:
            raise ValueError('预分析结果的候选文件与当前任务不一致')
        if state.get('state') == 'error':
            return state
        config = {"configurable": {"thread_id": session_id}, "recursion_limit": 100}
        return self.agent_invoke.invoke(state, config=config)
