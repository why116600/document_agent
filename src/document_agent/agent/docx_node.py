from pydantic import BaseModel, Field
from docx import Document
import json

from llm_client import llm_invoke, llm_model_invoke
from agent_core import AgentState
from document_agent.write_tool.word_tool import convert_node,replace_node_with_data,ParagraphItem,TableItem,DocxRoot

class DocxAction(BaseModel):# 定义一次写文档的动作
    action_type : str =Field(description="文档写作的类型，new表示全新的写作，last表示改写前一次生成的文档，rewrite表示重写或改写用户指定的文档")
    rewrite_path : str = Field(description="重写或改写的目标文档的路径")
    save_path : str  = Field(description="要保存的文档路径，如果没有，就默认1.docx")

def create_new_docx_node(llm):#从零编写文档的节点
    def docx_node(state : AgentState) -> AgentState:#写docx的节点
        s=state["messages"]
        # input_files=state['input_file_path']
        # file_list_str="\n".join(input_files)
        retrieved_items=state["retrieved_content"]
        retrieved_info=""
        if retrieved_items is not None and len(retrieved_items)>0:
            retrieved_info="目前已知：\n"+"\n".join(retrieved_items)
        prompt=(
            "你是文档写作助手，现在需要用户输入和已知信息列出整个文档的各个章节\n"
            "如果某些内容可以按表格的形式输出，则按一个章节来输出\n"
            f"用户输入：{s}\n"
            f"{retrieved_info}\n"
            "输出结果按照json的列表格式输出章节列表，仅输出json字符串结果，不要输出其它内容"
        )
        res=llm_invoke(llm,prompt)
        section_str=getattr(res,"content")
        if section_str is None:
            return {**state,"state":"error","error":"规划文档章节时，模型输出错误"}
        print("分解结果：",section_str)
        try:
            sections=json.loads(section_str)
        except Exception as e:
            return {**state,"state":"error","error":f"{e}"}
            
        doc=Document()
        for section in sections:
            print(f"编写{section}章节")
            prompt=(
                "你是一个写作系统的智能助手，需要根据章节主旨以及源信息进行文档生成，包括段落和表格\n"
                "文档字体上，除了用户的特殊要求，正文不加粗，标题加粗\n"
                f"章节主旨：{section}\n"
                f"{retrieved_info}\n"
            )
            res=llm_model_invoke(llm,prompt,DocxRoot)
            if res is None:
                print("写作失败")
                return {**state,"error":"写作失败","state":"error"}
            for item in res.items:
                if item.type=="paragraph":
                    node=doc.add_paragraph("")
                    replace_node_with_data(node,item.Paragraph)
                elif item.type=="table":
                    node=doc.add_table(rows=0,cols=0)
                    replace_node_with_data(node,item.Table)
        doc.save("1.docx")
            
        # if action is None:
        #     print("识别用户写作意图失败")
        #     return {**state,"error":"识别用户写作意图失败","state":"error"}
        # if action.action_type=="new":
        #     prompt=(
        #         "你是一个写作系统的智能助手，需要根据用户的要求以及源信息进行文档生成，包括段落和表格\n"
        #         "文档字体上，除了用户的特殊要求，正文不加粗，标题加粗\n"
        #         f"用户的要求：{s}\n"
        #         f"{retrieved_info}\n"
        #     )
        #     res=llm_model_invoke(llm,prompt,DocxRoot)
        #     if res is None:
        #         print("写作失败")
        #         return {**state,"error":"写作失败","state":"error"}
        #     doc = Document()
        #     for item in res.items:
        #         if item.type=="paragraph":
        #             node=doc.add_paragraph("")
        #             replace_node_with_data(node,item.Paragraph)
        #         elif item.type=="table":
        #             node=doc.add_table(rows=0,cols=0)
        #             replace_node_with_data(node,item.Table)
        #     doc.save(action.save_path)
        return {**state,"state":"succeeded"}
    return docx_node
            