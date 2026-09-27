from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, TypedDict, Annotated, Tuple
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

try:
    from document_agent.agent.llm_client import get_deepseek_llm
except ImportError:
    from llm_client import get_deepseek_llm

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    state: str
    retrieve_tool: str #要使用的检索工具
    retrieve_target: str#检索目标
    retrieve_params: Optional[Dict[str,str]]
    retrieve_count_limit: int#检索的限制次数
    file_retrived_items: Optional[Dict[Tuple,str]]#检索文档、关键词到检索结果的映射
    retrieved_content: List[str]#检索出来的内容
    input_file_path: List[str]#用户提供的参考文件路径
    file_summaries: Dict[str, str]#文件路径到文件摘要的映射
    gdb_label: str#图数据库节点标签；空串表示未接入图数据库（knowledge 工具不可用）
    user_intent: Optional[str]#用户写作意图，new表示全新写作，rewrite表示改写
    rewrite_file: Optional[str]#待改写的文档路径
    rewrite_mode: Optional[str]#改写模式：patch（局部修补）、replace（查找替换）、global（全局重塑/逐章润色）、format（仅格式规范化，文字一字不改）；有步骤清单时等于第一步的模式
    rewrite_steps: List[Dict[str, Any]]#有序改写步骤清单：复合指令由大模型拆成多步，每步含 mode/target/instruction/replace_pairs，改写节点逐步重建快照后依次执行
    replace_pairs: Optional[Dict[str, str]]#replace 模式下旧词到新词的映射
    save_path: str#输出文档的保存路径，为空时自动生成
    file_roles: Optional[Dict[str, str]]#文件角色标签映射（如待修改目标文档、辅助参考/风格模板）
    gdb_available: bool#图数据库是否可用，决定 knowledge 工具是否对检索智能体开放
    error: Optional[str]#错误结果


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
    return route_intent(state)

class AgentCore:
    def __init__(self):
        self.llm=get_deepseek_llm()
        self.graph=None
        self.agent_invoke=None
        self.checkpointer=InMemorySaver()#使用检查点记录会话内容
        
    def build_graph(self, gdb=None): # 构建agent图
        try:
            from document_agent.agent.intent_node import create_intent_node, route_after_intent
            from document_agent.agent.summary_node import create_summary_node
            from document_agent.agent.retrieve_node import create_retrieve_node
            from document_agent.agent.docx_node import create_new_docx_node
            from document_agent.agent.rewrite_node import create_rewrite_node
            from document_agent.agent.file_retrieve_node import create_file_retrieve_node
        except ImportError:
            from intent_node import create_intent_node, route_after_intent
            from summary_node import create_summary_node
            from retrieve_node import create_retrieve_node
            from docx_node import create_new_docx_node
            from rewrite_node import create_rewrite_node
            from file_retrieve_node import create_file_retrieve_node

        try:
            from document_agent.agent.gdb_retrieve_node import create_gdb_retrieve_node
        except ImportError:
            try:
                from gdb_retrieve_node import create_gdb_retrieve_node
            except ImportError:
                create_gdb_retrieve_node = None

        self.graph = StateGraph(AgentState)
        self.graph.add_node("intent", create_intent_node(self.llm))
        self.graph.add_node("summary", create_summary_node(self.llm))
        self.graph.add_node("retrieve", create_retrieve_node(self.llm))
        self.graph.add_node("retrieve_file", create_file_retrieve_node(self.llm))

        # =====================================================================
        # 【gdb 节点可选挂载与平滑降级说明】
        # 当前为了便于在未接入 Memgraph 或纯本地文档场景下调试，
        # 当 gdb 为 None 时挂载 dummy 提示节点，避免空指针崩溃。
        #
        # >>> 后期若正式部署上线、强制要求图数据库服务时，可将本 if-else 降级代码注释掉，
        # >>> 恢复为下方原版的强制挂载代码：
        # self.graph.add_node("retrieve_gdb", create_gdb_retrieve_node(self.llm, gdb))
        # =====================================================================
        if gdb is not None and create_gdb_retrieve_node is not None:
            self.graph.add_node("retrieve_gdb", create_gdb_retrieve_node(self.llm, gdb))
            self._gdb_available = True
        else:
            def dummy_gdb_retrieve(state: AgentState) -> AgentState:
                print("【知识库检索】当前未接入图数据库，跳过图检索")
                retrieved_items = list(state.get("retrieved_content") or [])
                retrieved_items.append(
                    "【系统提示】系统未配置图数据库，knowledge 工具当前不可用，"
                    "请勿再次调用 knowledge 工具；若参考信息已足够，请直接调用 end 结束检索。"
                )
                return {**state, "retrieved_content": retrieved_items}
            self.graph.add_node("retrieve_gdb", dummy_gdb_retrieve)
            self._gdb_available = False

        self.graph.add_node("new_docx", create_new_docx_node(self.llm))
        self.graph.add_node("rewrite_docx", create_rewrite_node(self.llm))
        self.graph.set_entry_point("intent")
        #意图识别出错时直接结束流程
        self.graph.add_conditional_edges(
            "intent",
            route_after_intent,
            {"continue":"summary","end":END},
        )
        self.graph.add_edge("summary","retrieve")
        self.graph.add_edge("retrieve_file","retrieve")
        self.graph.add_edge("retrieve_gdb","retrieve")
        #按检索工具与用户意图决定流转（继续检索 / 改写 / 新建 / 结束）
        self.graph.add_conditional_edges(
            "retrieve",
            route_retrieve,
            {"rewrite":"rewrite_docx","new":"new_docx","file":"retrieve_file","knowledge":"retrieve_gdb","end":END},
        )
        self.graph.add_edge("new_docx",END)
        self.graph.add_edge("rewrite_docx",END)
        self.agent_invoke=self.graph.compile(checkpointer=self.checkpointer)
        
    def invoke(self, user_input: str, input_file_path: Optional[List[str]] = None,
               session_id: str = "default_session") -> AgentState:
        if self.agent_invoke is None:
            raise ValueError("未初始化agent的图")
        config = {"configurable": {"thread_id": session_id}}
        return self.agent_invoke.invoke(
            {
                "messages": [{"role": "user", "content": user_input}],
                "retrieve_target": "",
                "state": "start",
                "input_file_path": list(input_file_path or []),
                "file_summaries": {},
                # 未接入图数据库时 gdb_label 为空串，检索节点在无参考文件时可直接跳过检索，省一次模型空转
                "gdb_label": "global" if bool(getattr(self, "_gdb_available", False)) else "",
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
                "save_path": "",
                "file_roles": {},
                "gdb_available": bool(getattr(self, "_gdb_available", False)),
                "error": None,
            },
            config=config
        )
        
if __name__ == "__main__":
    input_file_path=sys.argv[1:]
    user_input=input("请输入用户问题：")
    agent_core=AgentCore()
    agent_core.build_graph()
    result=agent_core.invoke(user_input, input_file_path)
    # print("检索出来的内容：", result["retrieved_content"])
    print("运行结果：",result.get("state",""))