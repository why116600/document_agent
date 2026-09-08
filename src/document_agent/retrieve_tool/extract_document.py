from pathlib import Path
from document_agent.retrieve_tool.excel_tools import ExcelToHtmlConverter
from document_agent.retrieve_tool.pdf_tools import extract_pdf_to_html
from document_agent.retrieve_tool.extract_word_text import extract_text_with_tables

def extract_document_content(file_path: str) -> str:
    path_obj = Path(file_path)
    if not path_obj.exists():
        return None
    if path_obj.suffix==".pdf":
        content=extract_pdf_to_html(file_path)
    elif path_obj.suffix in [".docx", ".doc"]:
        content=extract_text_with_tables(file_path)
    elif path_obj.suffix in [".xlsx", ".xls"]:
        converter = ExcelToHtmlConverter(file_path)
        content = converter.generate_html()
    elif path_obj.suffix in [".txt",".json",".csv",".md"]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        print(f"不支持的文件类型：{path_obj.suffix}")
        return None
    return content