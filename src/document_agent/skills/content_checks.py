"""可复用的文字检查，保留否定、条件、单位等语境。"""
import re


def strip_heading_number(text):
    return re.sub(r"^\s*(?:[一二三四五六七八九十百]+[、．.]|[（(][一二三四五六七八九十\d]+[）)]|\d+[、．.])\s*", "", text).strip()


def normalize_heading(text):
    return re.sub(r"\s+", "", strip_heading_number(text)).lower()


def placeholder_hits(text):
    hits = re.findall(r"\b(?:TODO|TBD|XXX)\b|此处省略|待填写|待完善", text, re.I)
    for match in re.finditer(r"待补充|待确认|后续补充", text):
        # 正常的后续行动和条件句不是正文占位符。
        tail = text[match.end():match.end() + 14]
        if re.match(r"(?:资料|数据|材料|明细|信息|文件|内容后|后再|并核实)", tail):
            continue
        hits.append(match.group())
    return hits


def unsupported_assertions(text):
    flagged = []
    for sentence in re.split(r"[。！？\n]", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        # 否定推断、说明缺失，以及要求未来核查的建议，不是已发生事实/预测断言。
        cleaned = re.sub(r"(?:不作|不做|不对.{0,15}|不能|无法|不足以|不宜|不应|尚不能|未能).{0,12}(?:推测|判断|预测|预计|断言)", "", sentence)
        if re.search(r"无法|不能|不作|不做|不宜|不足以", sentence) and not re.search(r"预计|有望|将会|未来|可能", cleaned):
            continue
        if re.search(r"建议|应当|需要|须在|取得.+后|补充.+后", sentence) and not re.search(r"已经|已导致|已发生|必然|一定会", sentence):
            continue
        if re.search(r"预计|预测|有望|推测|判断|将会|未来|可能", cleaned):
            flagged.append(sentence)
    return flagged
