from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, TypedDict, Tuple, Annotated
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from llm_client import get_deepseek_llm

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    state: str
    retrieve_target: str#检索目标
    retrieved_content: List[str]# 检索出来的内容
    input_file_path: List[str]#用户提供的作为参考内容的文件路径
    file_summaries: Dict[str, str]#文件路径到文件摘要的映射
    user_intent: Optional[str]#用户写作意图，new表示全新写作，rewrite表示改写
    rewrite_file: Optional[str]#重写文档的文件路径
    save_path: str#输出文档的保存路径，为空时自动生成
    error: Optional[str]#错误结果
    
def route_intent(state: AgentState) -> str:#检索完成后根据用户意图选择改写已有文档还是从零编写文档
    if state.get("user_intent") == "rewrite" and state.get("rewrite_file"):
        return "rewrite"
    return "new"

class AgentCore:
    def __init__(self):
        self.llm=get_deepseek_llm()
        self.graph=None
        self.agent_invoke=None
        self.checkpointer=InMemorySaver()#使用检查点记录会话内容
        
    def build_graph(self):#构建agent图
        from intent_node import create_intent_node, route_after_intent
        from summary_node import create_summary_node
        from retrieve_node import create_retrieve_node
        from docx_node import create_new_docx_node
        from rewrite_node import create_rewrite_node
        self.graph=StateGraph(AgentState)
        self.graph.add_node("intent", create_intent_node(self.llm))
        self.graph.add_node("summary", create_summary_node(self.llm))
        self.graph.add_node("retrieve", create_retrieve_node(self.llm))
        self.graph.add_node("new_docx",create_new_docx_node(self.llm))
        self.graph.add_node("rewrite_docx",create_rewrite_node(self.llm))
        self.graph.set_entry_point("intent")
        #意图识别出错时直接结束流程
        self.graph.add_conditional_edges(
            "intent",
            route_after_intent,
            {"continue":"summary","end":END},
        )
        self.graph.add_edge("summary","retrieve")
        #检索完成后根据用户意图选择改写已有文档还是从零编写文档
        self.graph.add_conditional_edges(
            "retrieve",
            route_intent,
            {"rewrite":"rewrite_docx","new":"new_docx"},
        )
        self.graph.add_edge("new_docx",END)
        self.graph.add_edge("rewrite_docx",END)
        self.agent_invoke=self.graph.compile(checkpointer=self.checkpointer)
        
    def invoke(self,user_input:str, input_file_path:List[str],session_id: str="default_session",
               rewrite_file: Optional[str]=None, save_path: Optional[str]=None,
               op: str="auto") -> AgentState:
        if self.agent_invoke is None:
            raise ValueError("未初始化agent的图")
        config = {"configurable": {"thread_id": session_id}}
        return self.agent_invoke.invoke(
            {
                "messages": [{"role": "user", "content": user_input}],
                "retrieve_target": "",
                "state": "start",
                "input_file_path": input_file_path,
                "file_summaries": {},
                "retrieved_content": [],
                "user_intent": "new" if op=="new" else ("rewrite" if (rewrite_file or op=="rewrite") else None),
                "rewrite_file": rewrite_file,
                "save_path": save_path or "",
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