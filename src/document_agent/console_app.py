import argparse
import os
import sys
<<<<<<< HEAD
from pathlib import Path

import document_agent.agent.agent_core as agent_core
from document_agent.agent.intent_node import SUPPORTED_REFERENCE_SUFFIXES


def parse_args():
    parser = argparse.ArgumentParser(
        prog="python -m document_agent.console_app",
        description=(
            "智能文档写作助手 (Document Agent)\n"
            "沿用命令行参数传参，终端输入文件路径时可按 Tab 键自动补齐。\n"
            "支持根据参考资料全新生成文档，或针对已有 Word 文档按三层模式（局部修补、全局替换、全局重塑）精准改写。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help=(
            "参考文件路径列表（可在命令行输入并按 Tab 自动补齐）。\n"
            f"支持的文件格式：{'、'.join(sorted(SUPPORTED_REFERENCE_SUFFIXES))}\n"
            "示例：python -m document_agent.console_app doc1.pdf doc2.xlsx"
        ),
    )
    parser.add_argument(
        "-w", "--rewrite",
        dest="rewrite_file",
        default=None,
        metavar="DOCX_PATH",
        help=(
            "需要改写的原 docx 文档路径（可在命令行输入并按 Tab 自动补齐）。\n"
            "提供该参数后进入【改写模式】；未提供时为【全新写作模式】。\n"
            "改写模式下若未指定 -o，默认原地覆盖该文档。"
        ),
    )
    parser.add_argument(
        "-m", "--mode",
        dest="rewrite_mode",
        choices=["auto", "patch", "replace", "global"],
        default="auto",
        help=(
            "改写模式（仅在提供 -w 时生效）：\n"
            "  auto    - 智能识别（默认）：根据您的自然语言要求自动匹配最佳改写策略\n"
            "  patch   - 局部微调修补：精准修改特定条款、增删段落或更新表格\n"
            "  replace - 全局查找替换：全文术语/专有名词/单位批量查找替换\n"
            "  global  - 全局重塑润色：全文逐章深度润色重写，复用原模板排版格式"
        ),
    )
    parser.add_argument(
        "--find",
        dest="find_text",
        default=None,
        metavar="TEXT",
        help="【全局查找替换模式】需要查找的目标原词（提供此参数时自动启用 replace 模式）",
    )
    parser.add_argument(
        "--replace-with",
        dest="replace_with",
        default=None,
        metavar="TEXT",
        help="【全局查找替换模式】替换后的新词",
    )
    parser.add_argument(
        "-o", "--output",
        dest="save_path",
        default=None,
        metavar="OUT_PATH",
        help=(
            "生成文档的保存路径（可在命令行输入并按 Tab 自动补齐）。\n"
            "新建模式下若未指定，默认自动保存在 data 目录下。\n"
            "改写模式下若未指定，默认原地覆盖原文档。"
        ),
    )
    parser.add_argument(
        "-p", "--prompt",
        dest="prompt",
        default=None,
        metavar="TEXT",
        help=(
            "写作要求或改写指令。\n"
            "若未在命令行中指定，程序将以交互形式在终端提示您输入。"
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 1. 校验改写文件参数与改写模式
    rewrite_file = None
    rewrite_mode = args.rewrite_mode or "auto"
    replace_pairs = {}

    # 校验 --find 与 --replace-with 参数组合
    if (args.find_text is None) != (args.replace_with is None):
        print("[错误] --find 与 --replace-with 必须同时提供")
        sys.exit(2)
    if args.find_text is not None and not args.find_text.strip():
        print("[错误] --find 查找内容不能为空或纯空白字符")
        sys.exit(2)
    if args.find_text is not None and not args.rewrite_file:
        print("[错误] 全局查找替换必须通过 -w/--rewrite 指定改写目标文档")
        sys.exit(2)

    # 如果指定了 --find，自动推断为 replace 模式
    if args.find_text is not None:
        rewrite_mode = "replace"
        replace_pairs[args.find_text] = args.replace_with

    if args.rewrite_file:
        rewrite_path = Path(args.rewrite_file)
        if not rewrite_path.exists():
            print(f"[错误] 指定的改写文档不存在：{args.rewrite_file}")
            sys.exit(1)
        if rewrite_path.suffix.lower() != ".docx":
            print(f"[错误] 改写目标只支持 .docx 格式的文档：{args.rewrite_file}")
            sys.exit(1)
        rewrite_file = str(rewrite_path.resolve())
    else:
        if rewrite_mode and rewrite_mode != "auto":
            print("[警告] 未提供改写目标文档（-w），-m/--mode 参数将被忽略，自动采用全新写作模式。")
        rewrite_mode = None

    # 2. 校验参考文件列表
    validated_files = []
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"[错误] 指定的参考文件不存在：{f}")
            sys.exit(1)
        if p.suffix.lower() not in SUPPORTED_REFERENCE_SUFFIXES:
            print(
                f"[错误] 不支持的参考文件格式：{f}\n"
                f"（支持的类型：{'、'.join(sorted(SUPPORTED_REFERENCE_SUFFIXES))}）"
            )
            sys.exit(1)
        # 若改写目标也被作为位置参数传入，避免重复当做参考文件
        if rewrite_file and p.resolve() == Path(rewrite_file).resolve():
            continue
        validated_files.append(str(p.resolve()))

    # 3. 获取用户写作/改写需求
    if args.prompt and args.prompt.strip():
        user_input = args.prompt.strip()
    elif replace_pairs:
        # 已通过命令行参数明确提供了 --find 与 --replace-with，无需额外输入
        user_input = "、".join([f"把全文的'{k}'替换为'{v}'" for k, v in replace_pairs.items()])
    else:
        if rewrite_file:
            if rewrite_mode == "replace":
                prompt_text = "请输入查找替换需求（例如：把全文的'甲方'换成'委托方'）："
            elif rewrite_mode == "global":
                prompt_text = "请输入全局重塑/全文润色要求（例如：将整篇文档润色为严谨的企业公文规范）："
            elif rewrite_mode == "patch":
                prompt_text = "请输入局部改写要求（例如：将第三条考核指标改为每月一次）："
            else:
                prompt_text = "请输入文档改写要求（例如：替换某词 / 修改具体条款 / 全文重构润色）："
        else:
            prompt_text = "请输入文档写作要求："
        user_input = input(prompt_text).strip()
        while not user_input:
            user_input = input("需求内容不能为空，请重新输入：").strip()

    # 4. 解析输出路径与操作类型
    save_path = str(Path(args.save_path).resolve()) if args.save_path else None
    op = "rewrite" if rewrite_file else "new"

    mode_label = {
        "auto": "智能识别 (auto)",
        "patch": "局部微调修补 (patch)",
        "replace": "全局查找替换 (replace)",
        "global": "全局重塑润色 (global)",
    }.get(rewrite_mode, "全新写作")

    print("\n" + "=" * 50)
    print(f"工作模式: {'改写文档 -> ' + mode_label if op == 'rewrite' else '全新写作'}")
    if rewrite_file:
        print(f"改写目标: {rewrite_file}")
    if replace_pairs:
        print(f"替换规则: {replace_pairs}")
    if validated_files:
        print(f"参考文件: {validated_files}")
    if save_path:
        print(f"输出目标: {save_path}")
    print(f"用户需求: {user_input}")
    print("=" * 50 + "\n")

    # 5. 执行工作流
    core = agent_core.AgentCore()
    core.build_graph()
    result = core.invoke(
        user_input=user_input,
        input_file_path=validated_files,
        rewrite_file=rewrite_file,
        save_path=save_path,
        rewrite_mode=rewrite_mode or "auto",
        replace_pairs=replace_pairs,
    )

    # 6. 输出结果反馈
    if result.get("state") == "succeeded":
        print("\n[成功] 文档已保存至：", result.get("save_path") or "")
    else:
        print("\n[失败] 运行结果：", result.get("state") or "", result.get("error") or "")


if __name__ == "__main__":
    main()
=======
import json
sys.path.append(os.path.join(os.path.dirname(__file__), 'agent'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'retrieve_tool'))

import document_agent.agent.agent_core as agent_core
from document_agent.memory.neo_graph_db import MemgraphNodeManager


if __name__ == "__main__":
    if not os.path.exists("setting.json"):
        print("没找到配置文件")
        sys.exit(-1)
    with open("setting.json","r") as fp:
        setstr=fp.read()
    settings=json.loads(setstr)
    gdb=MemgraphNodeManager(uri=settings["gdb_uri"],model_name=settings["embedding_model"])
    #改写目标、参考文件、输出路径都由自然语言描述，由intent节点自动识别
    user_input = input("请输入你的需求（自然语言）：")
    core = agent_core.AgentCore()
    core.build_graph(gdb)
    result = core.invoke(user_input, sys.argv[1:])
    # print("检索出来的内容：\n","\n\n\n".join(result["retrieved_content"]))
    if result.get("state") == "succeeded":
        print("文档已保存到：", result.get("save_path") or "")
    else:
        print("运行结果：", result.get("state") or "", result.get("error") or "")
>>>>>>> 418ae08453f505f34528228fa8cf69e129087893
