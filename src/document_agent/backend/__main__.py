"""命令行入口：``python -m document_agent.backend``。

用法：
    cd src
    python -m document_agent.backend                     # 按 setting.json 的配置启动
    python -m document_agent.backend --host 0.0.0.0      # 临时覆盖监听地址
    python -m document_agent.backend --port 9000         # 临时覆盖端口
    python -m document_agent.backend --print-port        # 打印最终端口（供启动脚本使用）
    python -m document_agent.backend --print-endpoint    # 打印 host|port|访问地址
    python -m document_agent.backend --show-config       # 打印监听配置（JSON）

监听地址优先级：命令行 ``--host`` > ``setting.json`` 的 ``web_allow_remote``
（false=127.0.0.1 仅本机，true=0.0.0.0 允许远程）。
"""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument(
        "--host",
        default="",
        help="临时覆盖监听地址；不写则按 setting.json 的 web_allow_remote 决定",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="临时覆盖 setting.json 中的 web_port（不改变远程访问开关）",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="打印最终监听配置（JSON）后退出，不启动服务",
    )
    parser.add_argument(
        "--print-port",
        action="store_true",
        help="只打印最终监听端口后退出，供启动脚本读取配置用",
    )
    parser.add_argument(
        "--print-endpoint",
        action="store_true",
        help="打印 host|port|访问地址 后退出，供启动脚本读取监听配置",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn 日志级别",
    )
    args = parser.parse_args(argv)

    # 记录命令行覆盖，保证应用启动时解析出的监听配置与这里一致
    config.set_port_override(args.port)
    config.set_host_override(args.host)
    web = config.resolve_web_settings()

    if args.show_config:
        print(json.dumps(web, ensure_ascii=False))
        return 0
    if args.print_port:
        print(web["port"])
        return 0
    if args.print_endpoint:
        print(f"{web['host']}|{web['port']}|{web['local_url']}")
        return 0

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖 uvicorn，请先执行：pip install fastapi uvicorn python-multipart", file=sys.stderr)
        return 1

    from document_agent.backend.app import app

    mode = "允许远程访问" if web["allow_remote"] else "仅本机访问"
    print(f"正在启动文档智能体 Web 服务：{web['local_url']}（{mode}）")
    uvicorn.run(app, host=web["host"], port=web["port"], log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
