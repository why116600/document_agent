"""证据记录工具：ID 分配、文件摘要、原文定位偏移与数值提取。

证据记录由检索节点写入 ``evidence_records``，验收节点据此做来源与数值核对。
所有偏移都基于「去除空白并转小写」的规范化文本，避免换行与全半角差异导致定位失败。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

EXTRACTION_VERSION = "v1"

_WS_PATTERN = re.compile(r"\s+")
_EVIDENCE_ID_PATTERN = re.compile(r"E-(\d+)")
_NUMBER_PATTERN = re.compile(r"\d+(?:[,\uFF0C]\d+)*(?:\.\d+)?%?")
_DIGEST_CACHE: Dict[Tuple[str, int, int], str] = {}


def normalize_text(text: str) -> str:
    """去除空白并转小写，作为偏移计算与片段比对的统一口径。"""
    return _WS_PATTERN.sub("", str(text or "")).lower()


def file_digest(path: str | Path) -> str:
    """原始文件字节的 sha256；按 (路径, 大小, mtime) 缓存，内容变更后自动失效。"""
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return ""
    key = (str(target.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _DIGEST_CACHE.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    try:
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return ""
    value = f"sha256:{digest.hexdigest()}"
    _DIGEST_CACHE[key] = value
    return value


def ensure_evidence_ids(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给缺少 evidence_id 的记录补 ``E-0001`` 形式的编号，已有编号保持不变。"""
    items = list(records or [])
    maximum = 0
    for record in items:
        match = _EVIDENCE_ID_PATTERN.fullmatch(str(record.get("evidence_id") or ""))
        if match:
            maximum = max(maximum, int(match.group(1)))
    for record in items:
        if not record.get("evidence_id"):
            maximum += 1
            record["evidence_id"] = f"E-{maximum:04d}"
    return items


def text_offset(text: str, quote: str) -> Tuple[int, int]:
    """返回 ``quote`` 在规范化 ``text`` 中的 (起始, 结束) 字符偏移；定位失败返回 (-1, -1)。"""
    haystack = normalize_text(text)
    needle = normalize_text(quote)
    if not haystack or not needle:
        return -1, -1
    start = haystack.find(needle)
    if start < 0:
        return -1, -1
    return start, start + len(needle)


def paragraph_index(text: str, quote: str) -> int:
    """返回包含 ``quote`` 的段落序号（从 0 开始）；定位失败返回 -1。"""
    needle = normalize_text(quote)
    if not needle:
        return -1
    for index, paragraph in enumerate(str(text or "").split("\n")):
        if needle in normalize_text(paragraph):
            return index
    return -1


def _query_tokens(query: str) -> List[str]:
    normalized = normalize_text(query)
    if not normalized:
        return []
    if len(normalized) == 1:
        return [normalized]
    return [normalized[index:index + 2] for index in range(len(normalized) - 1)]


def locate_quote(text: str, quote: str, query: str = "") -> Tuple[int, int]:
    """依次用引文、查询式、查询词定位；全部失败时返回 (-1, -1)。"""
    for needle in (quote, query):
        start, end = text_offset(text, needle)
        if start >= 0:
            return start, end
    haystack = normalize_text(text)
    for token in _query_tokens(query):
        index = haystack.find(token)
        if index >= 0:
            return index, index + len(token)
    return -1, -1


def locate_paragraph(text: str, quote: str, query: str = "") -> int:
    """定位引文所在段落；失败时退化为包含任一查询词的段落。"""
    for needle in (quote, query):
        index = paragraph_index(text, needle)
        if index >= 0:
            return index
    tokens = _query_tokens(query)
    for index, paragraph in enumerate(str(text or "").split("\n")):
        normalized = normalize_text(paragraph)
        if any(token in normalized for token in tokens):
            return index
    return -1


def numbers_in(text: str) -> List[str]:
    """提取文本中的数值（含百分号），统一全角并忽略千分位差异。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    values: List[str] = []
    for match in _NUMBER_PATTERN.finditer(normalized):
        value = match.group().replace(",", "").replace("\uFF0C", "").rstrip(".")
        if value:
            values.append(value)
    return values
