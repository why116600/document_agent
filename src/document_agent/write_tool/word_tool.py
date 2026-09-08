from typing import Union, Optional, List
from pydantic import BaseModel, Field
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx.table import Table, _Cell
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import RGBColor, Pt
from lxml import etree
import officemath2latex
import math2docx

class TextRunItem(BaseModel):
    text: str = Field(description="文本内容")
    text_type: str = Field(description="文本类型，text表示普通文本，latex表示latex格式的公式")
    bold: bool = Field(default=False, description="是否加粗")
    italic: bool = Field(default=False, description="是否斜体")
    underline: bool = Field(default=False, description="是否下划线")
    font_size: int = Field(default=12, description="字体大小，单位为磅")
    font_color: str = Field(default="#000000", description="字体颜色，使用十六进制颜色代码")
    
class ParagraphItem(BaseModel):
    runs: List[TextRunItem] = Field(description="文本运行列表")
    alignment: str = Field(default="left", description="段落对齐方式，可选值：left, center, right, justify")
    spacing_before: int = Field(default=0, description="段前间距，单位为磅")
    spacing_after: int = Field(default=0, description="段后间距，单位为磅")
    line_spacing: float = Field(default=1.0, description="行间距倍数，例如 1.0 表示单倍行距，2.0 表示双倍行距")
    
class GridItem(BaseModel):
    content: List[ParagraphItem] = Field(description="单元格内容，包含一个或多个段落")
    row : int = Field(default=0, description="单元格所在的行索引，从0开始")
    col : int = Field(default=0, description="单元格所在的列索引，从0开始")
    row_span: int = Field(default=1, description="单元格跨越的行数")
    col_span: int = Field(default=1, description="单元格跨越的列数")
    
class TableItem(BaseModel):
    rows: int = Field(description="表格的行数")
    cols: int = Field(description="表格的列数")
    grid: List[GridItem] = Field(description="表格的网格内容，包含每个单元格的内容和位置")
    
class DocxItem(BaseModel):
    type: str = Field(description="文档元素类型，paragraph表示段落文本，table表示表格")
    Paragraph: Optional[ParagraphItem] = Field(default=None, description="段落文本内容")
    Table: Optional[TableItem] = Field(default=None, description="表格内容")
    
class DocxRoot(BaseModel):
    items: List[DocxItem] = Field(description="文档内容列表，每个元素表示一段文本或一个表格")
    
# ---------- 转换辅助函数 ----------

def _rgb_to_hex(rgb: RGBColor) -> str:
    """将 RGBColor 对象转换为 '#RRGGBB' 格式的十六进制字符串"""
    if rgb is None:
        return "#000000"
    return f"#{rgb.rgb:06x}"

def _get_alignment(para: Paragraph) -> str:
    """将 WD_ALIGN_PARAGRAPH 枚举映射为字符串"""
    align = para.paragraph_format.alignment
    if align == WD_ALIGN_PARAGRAPH.LEFT:
        return "left"
    elif align == WD_ALIGN_PARAGRAPH.CENTER:
        return "center"
    elif align == WD_ALIGN_PARAGRAPH.RIGHT:
        return "right"
    elif align == WD_ALIGN_PARAGRAPH.JUSTIFY:
        return "justify"
    else:
        return "left"  # 默认

def _get_pt_value(val) -> int:
    """从 python-docx 的长度对象（如 Pt）中提取整数值（磅）"""
    if val is None:
        return 0
    if isinstance(val, Pt):
        return int(val.pt)
    return int(val)  # 兜底

def hex_to_rgb(hex_color: str) -> RGBColor:
    """将 '#RRGGBB' 转为 RGBColor 对象"""
    hex_color = hex_color.lstrip('#')
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return RGBColor(r, g, b)

def alignment_str_to_enum(align_str: str) -> WD_ALIGN_PARAGRAPH:
    """将对齐字符串映射为 WD_ALIGN_PARAGRAPH 枚举"""
    mapping = {
        'left': WD_ALIGN_PARAGRAPH.LEFT,
        'center': WD_ALIGN_PARAGRAPH.CENTER,
        'right': WD_ALIGN_PARAGRAPH.RIGHT,
        'justify': WD_ALIGN_PARAGRAPH.JUSTIFY,
    }
    return mapping.get(align_str, WD_ALIGN_PARAGRAPH.LEFT)

def convert_run(run: Run) -> TextRunItem:
    """将单个 Run 转换为 TextRunItem"""
    return TextRunItem(
        text=run.text,
        text_type="text",
        bold=run.font.bold if run.font.bold is not None else False,
        italic=run.font.italic if run.font.italic is not None else False,
        underline=run.font.underline if run.font.underline is not None else False,
        font_size=_get_pt_value(run.font.size) if run.font.size else 12,
        font_color=_rgb_to_hex(run.font.color.rgb) if run.font.color else "#000000"
    )

def convert_paragraph(para: Paragraph) -> ParagraphItem:
    """将 Paragraph 转换为 ParagraphItem，保留其所有 Run 的格式"""
    runs=[]
    for child in para._element.iterchildren():
        tag=child.tag
        if tag==qn("w:r"):
            run = Run(child, para)
            runs.append(convert_run(run))
        elif tag in (qn('m:oMath'), qn('m:oMathPara')):
            omml_str= etree.tostring(child, encoding='unicode',pretty_print=True)
            latex = officemath2latex.process_math_string(omml_str)
            runs.append(TextRunItem(text=latex,text_type="latex"))
    return ParagraphItem(
        runs=runs,
        alignment=_get_alignment(para),
        spacing_before=_get_pt_value(para.paragraph_format.space_before),
        spacing_after=_get_pt_value(para.paragraph_format.space_after),
        line_spacing=para.paragraph_format.line_spacing or 1.0  # 若为 None 则默认为 1.0
    )

def convert_cell(cell: _Cell, row_idx: int, col_idx: int) -> GridItem:
    """将单元格转换为 GridItem，包含其中的所有段落"""
    return GridItem(
        content=[convert_paragraph(p) for p in cell.paragraphs],
        row=row_idx,
        col=col_idx,
        row_span=1,  # 将在表格转换时修正
        col_span=1   # 将在表格转换时修正
    )

def convert_table(table: Table) -> TableItem:
    """
    将 Table 转换为 TableItem，正确处理合并单元格。
    遍历时只记录每个合并区域的左上角单元格，并设置正确的 row_span 和 col_span。
    """
    rows = len(table.rows)
    cols = len(table.rows[0].cells) if rows > 0 else 0
    grid_items = []

    # 创建一个二维标记数组，记录某个网格位置是否已被处理（合并起始或已跳过）
    processed = [[False] * cols for _ in range(rows)]

    for r in range(rows):
        for c in range(cols):
            if processed[r][c]:
                continue

            cell = table.cell(r, c)
            # 获取合并信息
            vMerge = cell._tc.vMerge
            grid_span = cell._tc.grid_span

            # 计算合并跨度
            col_span = grid_span if grid_span is not None else 1
            # 计算纵向合并的行数：从当前行开始，找到连续 vMerge='continue' 的个数
            row_span = 1
            if vMerge == 'restart':
                # 向下寻找直到遇到 vMerge 为 None 或 'restart'
                for dr in range(r + 1, rows):
                    next_cell = table.cell(dr, c)
                    if next_cell._tc.vMerge == 'continue':
                        row_span += 1
                    else:
                        break
            # 标记所有被此合并覆盖的网格位置为已处理
            for rr in range(r, r + row_span):
                for cc in range(c, c + col_span):
                    if rr < rows and cc < cols:
                        processed[rr][cc] = True

            # 创建 GridItem（若单元格为空段落，content 可能为空列表，仍保留）
            grid_item = convert_cell(cell, r, c)
            grid_item.row_span = row_span
            grid_item.col_span = col_span
            grid_items.append(grid_item)

    return TableItem(rows=rows, cols=cols, grid=grid_items)

# ---------- 统一入口函数 ----------

def convert_node(node) -> Union[TextRunItem, ParagraphItem, TableItem]:
    """
    根据节点类型转换为对应的 Pydantic 模型对象。

    支持的类型：
        - Run          -> TextRunItem
        - Paragraph    -> ParagraphItem
        - Table        -> TableItem
    其它类型抛出 TypeError。
    """
    if isinstance(node, Run):
        return convert_run(node)
    elif isinstance(node, Paragraph):
        return convert_paragraph(node)
    elif isinstance(node, Table):
        return convert_table(node)
    else:
        raise TypeError(f"不支持的节点类型: {type(node)}")
    
def convert_docx(doc : Document) -> DocxRoot:
    result=DocxRoot()
    result.items=[]
    for node in doc.iter_inner_content():
        try:
            item=convert_node(node)
            if isinstance(item,ParagraphItem):
                ditem=DocxItem(type="paragraph",Paragraph=item)
            elif isinstance(item,TableItem):
                ditem=DocxItem(type="table",Table=item)
            else:
                continue
            result.items.append(ditem)
        except Exception:
            continue
    return result
    
    
def fill_table_from_grid(table: Table, grid_items):
    """
    根据 GridItem 列表填充表格，处理合并单元格。
    注意：此函数假设表格已创建好行列数，且 grid_items 只包含合并区域的左上角单元格。
    """
    # 先填充所有单元格的段落内容（不带合并）
    for grid_item in grid_items:
        row, col = grid_item.row, grid_item.col
        cell = table.cell(row, col)
        # 清空单元格原有段落（默认有一个空段落）
        for p in cell.paragraphs:
            p._element.clear_content()
        # 添加新的段落
        for para_item in grid_item.content:
            p = cell.add_paragraph()
            # 设置段落格式
            p.paragraph_format.alignment = alignment_str_to_enum(para_item.alignment)
            p.paragraph_format.space_before = Pt(para_item.spacing_before)
            p.paragraph_format.space_after = Pt(para_item.spacing_after)
            p.paragraph_format.line_spacing = para_item.line_spacing
            # 添加 runs
            for run_item in para_item.runs:
                if run_item.text_type=="latex":
                    math2docx.add_math(p, run_item.text)
                    continue
                run = p.add_run(run_item.text)
                run.font.bold = run_item.bold
                run.font.italic = run_item.italic
                run.font.underline = run_item.underline
                run.font.size = Pt(run_item.font_size)
                if run_item.font_color:
                    run.font.color.rgb = hex_to_rgb(run_item.font_color)
    # 处理合并（必须在内容填充之后，因为合并会改变单元格引用）
    for grid_item in grid_items:
        row, col = grid_item.row, grid_item.col
        row_span, col_span = grid_item.row_span, grid_item.col_span
        if row_span > 1 or col_span > 1:
            start_cell = table.cell(row, col)
            # 计算结束单元格位置
            end_row = row + row_span - 1
            end_col = col + col_span - 1
            end_cell = table.cell(end_row, end_col)
            # 合并
            start_cell.merge(end_cell)
            
def replace_node_with_data(node, new_data, doc=None):
    """
    原地替换文档中的节点（Paragraph 或 Table）为新数据定义的内容。

    参数：
        node: 当前节点（Paragraph 或 Table 对象）
        new_data: ParagraphItem 或 TableItem 对象
        doc: Document 对象（可选，若 node 未包含 part 属性则需传入）
    返回：
        替换后的新节点（Paragraph 或 Table 对象，对于段落仍返回原 node）
    """
    if doc is None:
        # 尝试从 node 获取文档
        if hasattr(node, 'part'):
            doc = node.part.document
        else:
            raise ValueError("无法获取 Document 对象，请传入 doc 参数")

    # ----- 替换段落 -----
    if isinstance(node, Paragraph) and isinstance(new_data, ParagraphItem):
        # 清空所有子元素（保留段落本身）
        node._element.clear_content()
        # 设置段落格式
        pf = node.paragraph_format
        pf.alignment = alignment_str_to_enum(new_data.alignment)
        pf.space_before = Pt(new_data.spacing_before)
        pf.space_after = Pt(new_data.spacing_after)
        pf.line_spacing = new_data.line_spacing
        # 添加新的 runs
        for run_item in new_data.runs:
            if run_item.text_type=="latex":
                print("生成公式：",run_item.text)
                math2docx.add_math(node, run_item.text)
                continue
            run = node.add_run(run_item.text)
            run.font.bold = run_item.bold
            run.font.italic = run_item.italic
            run.font.underline = run_item.underline
            run.font.size = Pt(run_item.font_size)
            run.font.color.rgb = hex_to_rgb(run_item.font_color)
        return node  # 返回原段落对象（已更新）

    # ----- 替换表格 -----
    elif isinstance(node, Table) and isinstance(new_data, TableItem):
        # 1. 在文档末尾创建新表格（填充内容）
        new_table = doc.add_table(rows=new_data.rows, cols=new_data.cols)
        fill_table_from_grid(new_table, new_data.grid)

        # 2. 获取新旧元素的父元素及索引
        old_element = node._element
        parent = old_element.getparent()
        index = parent.index(old_element)

        # 3. 将新表格的元素移动到旧表格的位置
        new_element = new_table._element
        parent.insert(index, new_element)

        # 4. 删除旧表格元素
        parent.remove(old_element)

        # 5. 清理：从 doc.tables 中删除旧表格的引用（可选）
        # 由于 doc.tables 是惰性列表，直接重新获取即可，无需手动移除

        # 返回新表格对象（需重新获取，因为原对象已失效）
        # 由于新元素已插入，我们可以重新获取
        # 但为了简便，我们可以返回 None，或通过 doc.tables 中查找
        # 这里返回 new_table 对象，但它现在已插入到正确位置
        return new_table

    else:
        raise TypeError(f"节点类型 {type(node)} 与数据 {type(new_data)} 不匹配")