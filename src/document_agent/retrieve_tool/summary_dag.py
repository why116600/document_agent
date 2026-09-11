import sys
import os
from pydantic import BaseModel, Field
from typing import List

sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

from document_agent.agent.llm_client import llm_invoke, llm_model_invoke

MAX_CHUNK_TEST_SIZE=10000
CHUNK_UNIT_SIZE=1000

class ChunkUnit(BaseModel):
    source_txt : str = Field(description="吸收的原文")
    complete_txt : str = Field(description="吸收并补充完整的文本")
    # used_length : int = Field(description="吸收的原文的字符个数")
    
    
def get_chunks(llm,source_txt:str,chunk_size=CHUNK_UNIT_SIZE):
    offset=0
    index=0
    result=[]
    retry=0
    while offset<len(source_txt):
        print(f"总长：{len(source_txt)}，当前进度：{offset}")
        txt=source_txt[offset:offset+chunk_size]
        # print("原文：\n",txt)
        think_prompt=(
            "你是文本切块的工具。要从原文开头吸收文本，直到遇到语义不完整的语句为止\n"
            "如果文本是html，在吸收完原文的情况下补充不完整的html标记\n"
            f"原文：{txt}\n"
            "请分析对原文的吸收思路"
        )
        res=llm_invoke(llm,think_prompt)
        analysis=getattr(res,"content","")
        if len(analysis)<=0:
            analysis="无"
        # else:
        #     print("分析思路：\n",analysis)
        prompt=(
            "你是文本切块的工具。请从原文开头吸收文本，直到遇到语义不完整的语句为止\n"
            "如果文本是html，在吸收完原文的情况下补充不完整的html标记\n"
            f"请严格按照以下分析思路进行切块：{analysis}\n"
            f"原文：{txt}"
        )
        unit=llm_model_invoke(llm,prompt,ChunkUnit)
        if unit is None:
            retry+=1
            if retry>=3:
                print("切块失败，原文：",txt)
                return result
        absorbed_length=len(unit.source_txt)
        if absorbed_length<=0:
            retry+=1
            if retry>=3:
                print("吸收长度为0，原文：",txt)
                return result
        # print("切块结果：\n",unit.complete_txt)
        # print("="*60,f"{index}-{offset}","="*60)
        result.append(unit)
        index+=1
        offset+=absorbed_length
        retry=0
    return result

def build_retrieve_dag(llm,initial_chunks : List[str],slide_size=3):#构建摘要有向无环图
    current_chunks=[{"content":unit,"backward":[]} for unit in initial_chunks]
    for _ in range(10):
        parent_nodes=[]
        for chunk in current_chunks:
            content=chunk.get("content","")
            if len(content)<=0:
                continue
            if len(content)<=100:#如果本来字数就少，就不需要压缩了
                chunk["summary"]=content
                continue
            summary_prompt=(
                f"你是一个文字处理助手，请原文的内容压缩至{len(content)//10}个字以内，仅输出压缩后的内容\n"
                f"原文：\n{content}"
            )
            res=llm_invoke(llm,summary_prompt)
            chunk["summary"]=getattr(res,"content","")#content[:len(content)//10]#
            # print("原文：",content[:100])
            # print("\n")
            # print("压缩后：",chunk["summary"])
            # print("\n\n")
        # 滑动窗口式合并
        n=len(current_chunks)-slide_size+1
        if n<=0:
            n=1
        for i in range(n):
            m=i+slide_size
            if m>len(current_chunks):
                m=len(current_chunks)
            print(f"截取{i}-{m}的节点到父节点上")
            backward=current_chunks[i:m]
            content="\n".join([chunk.get("summary","") for chunk in backward])
            parent={"content":content,"backward":backward}
            parent_nodes.append(parent)
        # print("="*80)
        if len(parent_nodes)==1:
            node=parent_nodes[0]
            if not "summary" in node.keys():
                content=node.get("content","")
                if len(content)<=100:#如果本来字数就少，就不需要压缩了
                    node["summary"]=content
                else:
                    summary_prompt=(
                        f"你是一个文字处理助手，请原文的内容压缩至{len(content)//10}个字以内，仅输出压缩后的内容\n"
                        f"原文：\n{content}"
                    )
                    res=llm_invoke(llm,summary_prompt)
                    node["summary"]=getattr(res,"content","")
            return parent_nodes
        if len(parent_nodes)<1:
            return []
        current_chunks=parent_nodes
    return current_chunks