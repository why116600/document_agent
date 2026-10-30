"""命令行入口：``python -m document_agent.backend``。

用法：
    cd src
    python -m document_agent.backend --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional


def _bootstrap_path() -> None:
    """支持 ``python src/document_agent/backend/__main__.py`` 这种直接按路径运行的方式。"""
    here = Path(__file__).resolve()
    src = here.parents[2]
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main(argv: Optional[List[str]] = None) -> int:
    _bootstrap_path()

    from document_agent.backend import config

    parser = argparse.ArgumentParser(
        prog="python -m document_agent.backend",
        description="启动文档智能体 Web 服务（FastAPI + 内置前端页面）",
    )
    parser.add_argument("--host", default=config.DEFAULT_HOST, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT, help="监听端口，默认 8000")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别",
    )
    args = parser.parse_args(argv)

    # 让应用里的启动横幅与健康检查拿到真实的监听地址
    config.DEFAULT_HOST = args.host
    config.DEFAULT_PORT = args.port

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖 uvicorn，请先执行：pip install fastapi uvicorn python-multipart", file=sys.stderr)
        return 1

    from document_agent.backend.app import app

    print(f"正在启动文档智能体 Web 服务：{config.server_url(args.host, args.port)}")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
