from pathlib import Path

def extract_document_content(file_path: str) -> str:
    """
    按文件后缀解析文档内容，转换为纯文本或HTML表格，返回None表示不支持的类型或文件不存在。

    解析工具采用懒加载：只有真正解析对应类型的文件时，才需要安装对应的依赖
    （例如解析pdf需要安装pdfplumber与camelot）。
    """
    path_obj = Path(file_path)
    if not path_obj.exists():
        print(f"文件不存在：{file_path}")
        return None
    suffix = path_obj.suffix.lower()
    if suffix == ".pdf":
        from document_agent.retrieve_tool.pdf_tools import extract_pdf_to_html
        content = extract_pdf_to_html(file_path)
    elif suffix in [".docx", ".doc"]:
        from document_agent.retrieve_tool.extract_word_text import extract_text_with_tables
        content = extract_text_with_tables(file_path)
    elif suffix in [".xlsx", ".xls"]:
        from document_agent.retrieve_tool.excel_tools import ExcelToHtmlConverter
        converter = ExcelToHtmlConverter(file_path)
        content = converter.generate_html()
    elif suffix in [".txt", ".json", ".csv", ".md"]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        print(f"不支持的文件类型：{suffix}")
        return None
    return content