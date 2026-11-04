#!/usr/bin/env python3
"""把 ``setting_example.json`` 合并进 ``setting.json``（冲突时保留 ``setting.json``）。

脚本只依赖 Python 标准库，可以直接单独运行::

    python ./merge.py                              # 在 src 目录下
    python src/merge.py                            # 在项目根目录
    python merge.py --dry-run                      # 只打印会改什么，不写文件
    python merge.py --example a.json --target b.json

合并规则
--------
1. 示例里有、``setting.json`` 里缺失的键：补进 ``setting.json``；
2. 两边都有的键（冲突）：保留 ``setting.json`` 的原值，示例值被忽略；
3. 两边都是对象（如 ``remote_kg``）：逐层递归，只补充缺失的叶子键；
4. 其它类型冲突（字符串、数字、布尔、数组）：整个保留 ``setting.json`` 的值；
5. 合并结果与 ``setting.json`` 现有内容一致时不写文件，避免无意义的改动；
6. 键顺序：先按示例文件的顺序，示例里没有的键按 ``setting.json`` 原有顺序跟在后面。

退出码：``0`` 成功（无论是否有改动），``1`` 出错（文件缺失、JSON 非法等）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 默认按脚本所在目录定位两个文件，因此从任何目录启动结果都一致
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_EXAMPLE = SCRIPT_DIR / "setting_example.json"
DEFAULT_TARGET = SCRIPT_DIR / "setting.json"

# 与仓库里现有 setting.json 的写法保持一致：4 空格缩进
INDENT = 4


class MergeError(Exception):
    """合并失败（文件缺失、JSON 非法、参数矛盾等），由 :func:`main` 转成退出码 1。"""


# --------------------------------------------------------------------------- 读取
def load_json(path: Path, *, required: bool) -> Optional[Dict[str, Any]]:
    """读取一个 JSON 对象文件。

    :param path: 文件路径
    :param required: 文件必须存在（``False`` 时缺失返回 ``None``）
    :raises MergeError: 文件缺失（required 时）、JSON 非法、顶层不是对象
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:  # utf-8-sig 兼容带 BOM 的文件
            text = fp.read()
    except FileNotFoundError:
        if required:
            raise MergeError(f"找不到文件：{path}")
        return None
    except OSError as exc:
        raise MergeError(f"读取 {path} 失败：{exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MergeError(
            f"{path.name} 不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise MergeError(f"{path.name} 的顶层必须是 JSON 对象（{{...}}），实际是 {type(data).__name__}")
    return data


def _same_value(left: Any, right: Any) -> bool:
    """比较两个 JSON 值是否等价（``True`` 与 ``1`` 视为不同，避免误判成“无需修改”）。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right


# --------------------------------------------------------------------------- 合并
def _dotted(path: Sequence[Any]) -> str:
    """把键路径拼成 ``remote_kg.enabled`` 这样的点号形式，便于打印。"""
    return ".".join(str(part) for part in path)


def merge_example(
    example: Dict[str, Any],
    target: Dict[str, Any],
    prefix: Tuple[Any, ...] = (),
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """把示例配置补进目标配置，冲突时以目标配置为准。

    :param example: ``setting_example.json`` 的内容（低优先级）
    :param target: ``setting.json`` 的内容（高优先级，冲突时保留）
    :param prefix: 递归时已走过的键路径
    :return: ``(合并结果, 补充的键, 冲突时被保留的键)``
    """
    merged: Dict[str, Any] = {}
    added: List[str] = []
    kept: List[str] = []

    for key, example_value in example.items():
        path = prefix + (key,)
        if key not in target:
            # 示例有、目标没有：补进去
            merged[key] = example_value
            added.append(_dotted(path))
            continue

        target_value = target[key]
        if isinstance(example_value, dict) and isinstance(target_value, dict):
            # 嵌套对象：逐层递归，只处理叶子键
            sub, sub_added, sub_kept = merge_example(example_value, target_value, path)
            merged[key] = sub
            added.extend(sub_added)
            kept.extend(sub_kept)
        else:
            # 冲突：保留目标（setting.json）的原值
            merged[key] = target_value
            if not _same_value(example_value, target_value):
                kept.append(_dotted(path))

    # 只在 setting.json 里出现的键原样保留，顺序跟在示例键之后
    for key, target_value in target.items():
        if key not in example:
            merged[key] = target_value

    return merged, added, kept


# --------------------------------------------------------------------------- 写入
def detect_newline(path: Path) -> str:
    """沿用现有文件的换行风格；读不到时按平台惯例（Windows CRLF / 其他 LF）。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return "\r\n" if os.name == "nt" else "\n"
    return "\r\n" if b"\r\n" in raw else "\n"


def write_json(path: Path, data: Dict[str, Any], newline: str) -> None:
    """先写临时文件再原子替换，避免中途失败留下半个 JSON。"""
    text = json.dumps(data, indent=INDENT, ensure_ascii=False) + "\n"
    if newline != "\n":
        # json.dumps 只会产生 \n，字符串内部的换行已被转义成两个字符，这里替换是安全的
        text = text.replace("\n", newline)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- 命令行
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge.py",
        description="把 setting_example.json 合并进 setting.json：缺失的键补齐，冲突时保留 setting.json。",
    )
    parser.add_argument(
        "-e",
        "--example",
        default=str(DEFAULT_EXAMPLE),
        metavar="PATH",
        help=f"示例配置文件（默认 {DEFAULT_EXAMPLE.name}，与脚本同目录）",
    )
    parser.add_argument(
        "-t",
        "--target",
        default=str(DEFAULT_TARGET),
        metavar="PATH",
        help=f"要更新到的配置文件（默认 {DEFAULT_TARGET.name}，与脚本同目录）",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="只打印合并结果，不写文件",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="成功时不打印合并详情",
    )
    return parser


def _print_report(
    example_path: Path,
    target_path: Path,
    added: List[str],
    kept: List[str],
) -> None:
    print(f"合并 {example_path} → {target_path}")
    if added:
        print(f"  补充缺失配置（{len(added)} 项）：{'、'.join(added)}")
    else:
        print("  补充缺失配置：无")
    if kept:
        print(f"  保留 setting.json 已有配置、忽略示例值（{len(kept)} 项）：{'、'.join(kept)}")
    else:
        print("  冲突项：无")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    example_path = Path(args.example).expanduser().resolve()
    target_path = Path(args.target).expanduser().resolve()

    try:
        if example_path == target_path:
            raise MergeError(f"示例文件与目标文件是同一个文件：{example_path}")

        example = load_json(example_path, required=True) or {}
        target_exists = target_path.is_file()
        target = load_json(target_path, required=False) or {}

        merged, added, kept = merge_example(example, target)
        changed = not target_exists or not _same_value(merged, target)

        if not args.quiet:
            _print_report(example_path, target_path, added, kept)

        if args.dry_run:
            print("（--dry-run）未写入任何文件。")
            return 0

        if not changed:
            print(f"{target_path.name} 内容已一致，无需写入。")
            return 0

        write_json(target_path, merged, detect_newline(target_path if target_exists else example_path))
        print(f"已写入 {target_path}")
        return 0
    except MergeError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[错误] 写入 {target_path} 失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
