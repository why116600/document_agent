import sys
import pdfplumber
import camelot

def extract_pdf_to_html(pdf_path : str) -> str:
    html_parts = []
    html_parts.append("<html><head><meta charset='utf-8'></head><body>")
    
    # 1. 使用 pdfplumber 提取普通文本
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
                for para in paragraphs:
                    html_parts.append(f"<p>{para}</p>")
            
            # 2. 使用 Camelot 提取当前页的表格
            # 注意：Camelot 按页码提取，页码从 1 开始
            try:
                tables = camelot.read_pdf(pdf_path, pages=str(page_num + 1), flavor='lattice')
                # 如果 lattice 没找到，尝试 stream 模式
                if len(tables) == 0:
                    tables = camelot.read_pdf(pdf_path, pages=str(page_num + 1), flavor='stream')
                
                for table in tables:
                    # Camelot 可以直接导出为 HTML 字符串
                    html_table = table.df.to_html(index=False, border=1)
                    html_parts.append(html_table)
            except Exception as e:
                # 如果当前页没有表格或解析出错，跳过
                pass
            
            html_parts.append('<hr style="border: 1px dashed #ccc;">')
    
    html_parts.append("</body></html>")
    
    return '\n'.join(html_parts)

if __name__=="__main__":
    html_path='output.html'
    res=extract_pdf_to_html(sys.argv[1])
    
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(res)
    
    print(f"HTML 文件已生成: {html_path}")