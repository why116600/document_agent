"""管理入口审计：追加写 JSONL，并记录当前系统操作者。"""

from __future__ import annotations

import getpass
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..store import _STORE_LOCK, resolve_skill_dir


def current_operator() -> str:
    """没有登录体系时，用当前系统用户名作为操作者。"""
    try:
        return getpass.getuser()
    except Exception:
        return os.getenv("USERNAME") or os.getenv("USER") or "local"


def audit(
    actor: str,
    action: str,
    skill_id: str = "",
    *,
    version: Optional[int] = None,
    result: str = "ok",
    detail: str = "",
) -> None:
    """追加一条审计记录；审计失败不阻断主操作。"""
    record: Dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event_id": uuid4().hex[:12],
        "actor": actor or current_operator(),
        "action": action,
        "skill_id": skill_id,
        "version": version,
        "result": result,
        "detail": detail[:500],
    }
    path = resolve_skill_dir() / ".audit" / f"audit-{datetime.now():%Y%m}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _STORE_LOCK:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
    except OSError as exc:
        print(f"[审计] 写入失败：{exc}")


def list_audit(skill_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    """按时间倒序返回审计记录。"""
    root = resolve_skill_dir() / ".audit"
    if not root.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(root.glob("audit-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if skill_id and str(item.get("skill_id") or "") != skill_id:
                continue
            records.append(item)
            if len(records) >= limit:
                return records
    return records
