"""轻量的文件类型定义。

独立成模块，避免 Web 后端做上传校验时触发智能体依赖（langgraph 等）的首次导入
（在本机实测首次导入约需 20 秒）。意图节点与后端共用同一份后缀清单。
"""

from __future__ import annotations

# 可作为参考文件的类型，与 extract_document 的解析能力保持一致
SUPPORTED_REFERENCE_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".json", ".csv", ".md"}

# 只有 .docx 可以作为改写目标
DOCX_SUFFIX = ".docx"

__all__ = ["SUPPORTED_REFERENCE_SUFFIXES", "DOCX_SUFFIX"]
