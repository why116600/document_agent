"""新建报告的确定性表格格式：重复表头、防止拆行及按内容分配列宽。"""
from math import sqrt

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Twips, RGBColor
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH


def _child(parent, tag, **attributes):
    node = parent.find(qn(tag))
    if node is None:
        node = OxmlElement(tag)
        parent.append(node)
    for key, value in attributes.items():
        node.set(qn('w:' + key), str(value))
    return node


def _column_widths(table, available, font_size):
    columns = len(table.columns)
    lengths = [8] * columns
    for row in table.rows:
        seen = set()
        for c, cell in enumerate(row.cells):
            if cell._tc in seen: continue
            seen.add(cell._tc)
            # 跨列说明与合并表头不按每一列重复计算文本权重。
            if cell._tc.grid_span > 1: continue
            lengths[c] = max(lengths[c], min(80, len(cell.text)))
    weights = [sqrt(length) for length in lengths]
    # 单列至少容纳3个中文字符；指标列至少4个，避免单字竖排。
    minimum = [int(font_size * 20 * (4 if c == 0 else 3) + 180) for c in range(columns)]
    if sum(minimum) > available:
        # 极宽表仍保留可打印宽度；该情形需排版审核选择横向/拆表。
        minimum = [available // columns] * columns
    spare = available - sum(minimum)
    widths = [m + int(spare * w / sum(weights)) for m, w in zip(minimum, weights)]
    widths[-1] += available - sum(widths)
    return widths


def format_report_tables(doc, skill=None):
    # 模板的表格布局由模板控制；默认排版只应用于没有模板的新文档。
    if skill is not None and getattr(skill, 'template_path', ''):
        return
    available = int((doc.sections[0].page_width - doc.sections[0].left_margin - doc.sections[0].right_margin) / 635)
    fmt = skill.structure.format if skill is not None else None
    for table in doc.tables:
        if not table.rows or not table.columns:
            continue
        table.autofit = False
        columns = len(table.columns)
        widths = _column_widths(table, available, fmt.body_size if fmt and fmt.body_size else 12)
        for column, width in zip(table.columns, widths):
            column.width = Twips(width)
        _child(table._tbl.tblPr, 'w:tblW', w=available, type='dxa')
        # 让Word使用普通行内表格，浮动表格不保证跨页重复表头。
        for floating in list(table._tbl.tblPr.findall(qn('w:tblpPr'))):
            table._tbl.tblPr.remove(floating)
        borders = _child(table._tbl.tblPr, 'w:tblBorders')
        for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
            _child(borders, 'w:' + edge, val='single', sz=4, color='D9D9D9')
        seen_cells = set()
        for index, row in enumerate(table.rows):
            props = row._tr.get_or_add_trPr()
            _child(props, 'w:cantSplit', val=1)
            if index == 0:
                _child(props, 'w:tblHeader', val=1)
            for c, cell in enumerate(row.cells):
                if cell._tc in seen_cells:
                    continue
                seen_cells.add(cell._tc)
                cell.width = Twips(sum(widths[c:c + cell._tc.grid_span]))
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                props = cell._tc.get_or_add_tcPr()
                margins = _child(props, 'w:tcMar')
                for side in ('top', 'bottom', 'left', 'right'):
                    _child(margins, 'w:' + side, w=90, type='dxa')
                _child(props, 'w:shd', fill='EAEAEA' if index == 0 else ('F8F8F8' if index % 2 == 0 else 'FFFFFF'))
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.first_line_indent = Pt(0)
                    paragraph.paragraph_format.space_before = Pt(2)
                    paragraph.paragraph_format.space_after = Pt(2)
                    paragraph.paragraph_format.line_spacing = 1.15
                    paragraph.paragraph_format.keep_with_next = index == 0
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if index == 0 else WD_ALIGN_PARAGRAPH.LEFT
                    for run in paragraph.runs:
                        if fmt and fmt.body_size:
                            run.font.size = Pt(fmt.body_size)
                        if fmt and fmt.body_font:
                            run.font.name = fmt.body_font
                            run._element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), fmt.body_font)
                        run.font.color.rgb = RGBColor(0, 0, 0)
                        if index == 0:
                            run.bold = True
                            run.font.color.rgb = RGBColor(0, 0, 0)
