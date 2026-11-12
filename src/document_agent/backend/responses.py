"""Web 响应类型：显式 UTF-8，避免部分客户端按 Latin-1 解码中文。"""

from fastapi.responses import JSONResponse


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


__all__ = ["UTF8JSONResponse"]
