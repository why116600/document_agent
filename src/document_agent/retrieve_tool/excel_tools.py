import os
from openpyxl import Workbook, load_workbook
from typing import Optional, Tuple, List, Union

def append_to_excel(file_path, data_list, headers=None):
    """
    向指定路径的 Excel 添加一行数据。
    :param file_path: 文件完整路径 (例如 'data/output.xlsx')
    :param data_list: 要写入的数据列表 (例如 ['张三', 25, '技术部'])
    :param headers: 表头列表 (仅在文件新建时生效，例如 ['姓名', '年龄', '部门'])
    """
    
    # 1. 检查并创建目录 (如果路径中包含子文件夹)
    directory = os.path.dirname(file_path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)
        print(f"📁 目录不存在，已创建: {directory}，原路径：{file_path}")

    # 2. 判断文件是否存在
    if not os.path.exists(file_path):
        # --- 情况 A: 文件不存在，创建新文件 ---
        wb = Workbook()
        ws = wb.active
        ws.title = "数据表"
        
        # 写入表头
        if headers:
            ws.append(headers)
            print(f"📝 文件新建，已写入表头: {headers}")
        else:
            print("⚠️ 文件新建，但未提供表头")
            
    else:
        # --- 情况 B: 文件存在，加载文件 ---
        wb = load_workbook(file_path)
        ws = wb.active
        print(f"📂 文件已存在，准备追加数据到: {file_path}")

    print("fuck excel!")
    # 3. 追加数据行
    print("adding data to excel:", data_list)
    for i,item in enumerate(data_list):
        print("item",i)
        print(item)
        if isinstance(item, list):
            s=item
        else:
            s=[str(item),]
        try:
            # print(f"添加数据项{i}:",s)
            ws.append(s)
        except Exception as e:
            print(f"❌ 写入 Excel 失败: {e}")

    # 4. 保存文件
    # print(f"💾 数据已写入 Excel，保存文件: {file_path}")
    wb.save(file_path)

class ExcelToHtmlConverter:
    """
    将 Excel 文件转换为 HTML 表格的工具类。
    内部使用 openpyxl（data_only=True），公式输出为计算结果（需文件已保存过）。
    """

    def __init__(self, excel_path: str):
        """
        初始化，加载 Excel 工作簿。
        :param excel_path: Excel 文件路径
        """
        print("打开excel表格：",excel_path)
        self._wb = load_workbook(excel_path, data_only=True)
        self._file_path = excel_path

    def get_sheet_names(self) -> List[str]:
        """返回所有工作表的名称列表。"""
        return self._wb.sheetnames

    def get_sheet_dimensions(self, sheet_name: Optional[str] = None) -> Tuple[int, int]:
        """
        获取指定工作表的行数和列数（基于已使用区域的最大行列）。
        :param sheet_name: 工作表名称，若为 None 则使用当前活动表
        :return: (行数, 列数)
        """
        ws = self._wb[sheet_name] if sheet_name else self._wb.active
        return ws.max_row, ws.max_column

    def generate_html(
        self,
        sheet_name: Optional[str] = None,
        row_start: Optional[int] = None,
        row_end: Optional[int] = None,
        col_start: Optional[int] = None,
        col_end: Optional[int] = None,
        output_path: Optional[str] = None
    ) -> Union[str, None]:
        """
        将指定工作表（或活动表）的指定区域转换为 HTML 表格。
        :param sheet_name:  工作表名称，None 表示活动表
        :param row_start:   起始行号（Excel 1-based），None 表示从第 1 行开始
        :param row_end:     结束行号（Excel 1-based），None 表示到最后一行
        :param col_start:   起始列号（Excel 1-based），None 表示从第 1 列开始
        :param col_end:     结束列号（Excel 1-based），None 表示到最后一列
        :param output_path: 若指定，将 HTML 写入文件；若为 None，则返回 HTML 字符串
        :return: 若 output_path 为 None，返回 HTML 字符串；否则返回输出路径
        """
        ws = self._wb[sheet_name] if sheet_name else self._wb.active

        # 使用 iter_rows 按指定范围提取数据（None 表示使用默认边界）
        rows = ws.iter_rows(
            min_row=row_start,
            max_row=row_end,
            min_col=col_start,
            max_col=col_end,
            values_only=True
        )

        # 构建表格 HTML
        html_lines = ['<table border="1" cellpadding="5" cellspacing="0">']
        for row in rows:
            html_lines.append('<tr>')
            for cell in row:
                value = cell if cell is not None else ''
                html_lines.append(f'<td>{value}</td>')
            html_lines.append('</tr>')
        html_lines.append('</table>')

        table_html = ''.join(html_lines)
        full_html = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Excel 表格</title></head>
<body>
{table_html}
</body>
</html>'''

        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(full_html)
            return output_path
        else:
            return full_html

    # 可选：支持上下文管理，自动释放资源（openpyxl 无需显式关闭）
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass