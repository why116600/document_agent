import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

from document_agent.agent.llm_client import get_deepseek_llm, llm_invoke, llm_model_invoke
import document_agent.agent.agent_core as agent_core


if __name__ == "__main__":
    input_file_path=sys.argv[1:]
    user_input=input("请输入用户问题：")
    agent_core=agent_core.AgentCore()
    agent_core.build_graph()
    result=agent_core.invoke(user_input, input_file_path)
    print("检索出来的内容：", result["retrieved_content"])