#!/usr/bin/env python3
"""
extract_text_from_docx.py - 从 Word 文档中提取纯文本，表格以 HTML 格式保存。
用法:
    python extract_text_from_docx.py <输入文件> [输出文件]
依赖:
    pip install python-docx
"""

import sys
import html
from docx import Document
from docx.oxml.ns import qn
from lxml import etree
import officemath2latex


def extract_text_with_tables(docx_path):
    """读取 .docx 文件，按顺序返回段落文本和表格 HTML 的列表。"""
    doc = Document(docx_path)
    output_lines = []

    # 遍历文档 body 的所有子元素，按顺序处理段落和表格
    for child in doc.element.body:
        tag = child.tag
        print("经过的tag:",tag)
        if tag == qn('w:p'):  # 段落
            # 使用 local-name() 避免命名空间参数问题
            # texts = child.xpath('.//*[local-name()="t"]')
            # text = ''.join(t.text for t in texts).strip()
            # if text:  # 忽略空段落
            #     output_lines.append(text)
            for subchild in child.iterchildren():
                subtag=subchild.tag
                if subtag==qn("w:r"):
                    subtexts = subchild.findall(qn('w:t'))
                    full_text = ''.join(t.text for t in subtexts if t.text)
                    output_lines.append(full_text)
                elif subtag in (qn('m:oMath'), qn('m:oMathPara')):
                    omml_str= etree.tostring(subchild, encoding='unicode',pretty_print=True)
                    latex = officemath2latex.process_math_string(omml_str)
                    output_lines.append(latex)
        elif tag == qn('w:tbl'):  # 表格
            rows = child.xpath('.//*[local-name()="tr"]')
            html_table = ['<table border="1">']
            for row in rows:
                cells = row.xpath('.//*[local-name()="tc"]')
                html_table.append('<tr>')
                for cell in cells:
                    cell_texts = cell.xpath('.//*[local-name()="t"]')
                    cell_text = ''.join(t.text for t in cell_texts)
                    cell_text = html.escape(cell_text)
                    html_table.append(f'<td>{cell_text}</td>')
                html_table.append('</tr>')
            html_table.append('</table>')
            output_lines.append(''.join(html_table))
        elif tag == qn('m:oMath'): #公式
            omml_str= etree.tostring(child, encoding='unicode',pretty_print=True)
            print("找到的公式：",omml_str)
            latex = officemath2latex.process_math_string(omml_str)
            output_lines.append(latex)

    return '\n'.join(output_lines)


def main():
    if len(sys.argv) < 2:
        print("用法: python extract_text_from_docx.py <输入文件> [输出文件]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        result = extract_text_with_tables(input_file)
    except FileNotFoundError:
        print(f"错误: 文件 '{input_file}' 不存在。")
        sys.exit(1)
    except Exception as e:
        print(f"处理文档时出错: {e}")
        sys.exit(1)

    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(result)
        print(f"结果已保存到 '{output_file}'")
    else:
        print(result)


if __name__ == '__main__':
    main()