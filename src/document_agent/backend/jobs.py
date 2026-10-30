"""后台生成任务、事件流与 stdout 日志捕获。

智能体的节点通过 ``print`` 输出进度（检索分析、章节规划、保存路径等），
控制台版本直接打印到屏幕；Web 版把这些输出按线程归属捕获到对应任务，
再通过 SSE 推送给前端，这样多个会话并发生成时日志不会串台。
"""

from __future__ import annotations

import io
import secrets
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# 任务保留上限，避免长时间运行后内存里堆积过多历史任务
_MAX_EVENTS_PER_JOB = 20000
_MAX_KEPT_JOBS = 200
_JOB_TTL_SECONDS = 2 * 60 * 60

_thread_sink = threading.local()
_router_lock = threading.Lock()
_router_installed = False


# ---------------------------------------------------------------------- stdout 捕获
class _SinkWriter(io.TextIOBase):
    """按线程把 ``print`` 的内容转给当前生成的 Job，其它线程照常输出。"""

    def __init__(self, original: Optional[Any]):
        self._original = original

    # 让依赖 encoding 的库不报错
    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._original, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]
        return getattr(self._original, "errors", None) or "replace"

    def write(self, text: str) -> int:  # type: ignore[override]
        sink: Optional[Callable[[str], None]] = getattr(_thread_sink, "sink", None)
        if sink is not None:
            try:
                sink(str(text))
            except Exception:
                pass
            return len(text)
        if self._original is not None:
            try:
                return self._original.write(text)
            except Exception:
                return len(text)
        return len(text)

    def flush(self) -> None:  # type: ignore[override]
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self) -> bool:  # type: ignore[override]
        return False

    def writable(self) -> bool:  # type: ignore[override]
        return True


def install_stdout_router() -> None:
    """进程内安装一次 stdout 分流器（重复调用无副作用）。"""
    global _router_installed
    with _router_lock:
        if _router_installed:
            return
        sys.stdout = _SinkWriter(sys.stdout)
        _router_installed = True


def bind_sink(sink: Callable[[str], None]) -> None:
    _thread_sink.sink = sink


def unbind_sink() -> None:
    _thread_sink.sink = None


# ---------------------------------------------------------------------- 任务
class Job:
    """一次文档生成任务：保存事件、完整日志与最终结果。"""

    def __init__(self, job_id: str, session_id: str):
        self.id = job_id
        self.session_id = session_id
        self.status = "running"  # running / succeeded / failed
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.events: List[Dict[str, Any]] = []
        self.lines: List[str] = []
        self._buffer = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 事件
    def emit(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self.events.append(event)
            self._trim_events()

    def _trim_events(self) -> None:
        if len(self.events) <= _MAX_EVENTS_PER_JOB:
            return
        overflow = len(self.events) - _MAX_EVENTS_PER_JOB
        self.events = self.events[overflow:]

    def append_log(self, text: str) -> None:
        """把 print 写入的文本按行切分成日志事件（保留未结束的半行）。"""
        with self._lock:
            self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
            chunks = self._buffer.split("\n")
            self._buffer = chunks.pop()
            for line in chunks:
                self._push_line(line)

    def _push_line(self, line: str) -> None:
        clean = line.rstrip()
        if not clean.strip():
            return
        self.lines.append(clean)
        self.events.append({"type": "log", "line": clean})
        self._trim_events()

    def _flush_buffer(self) -> None:
        with self._lock:
            if self._buffer.strip():
                self._push_line(self._buffer)
            self._buffer = ""

    # ------------------------------------------------------------------ 结束
    def finish(self, result: Dict[str, Any]) -> None:
        self._flush_buffer()
        with self._lock:
            self.status = "succeeded"
            self.result = result
            self.finished_at = time.time()
            self.events.append({"type": "done", "result": result})

    def fail(self, message: str) -> None:
        self._flush_buffer()
        with self._lock:
            self.status = "failed"
            self.error = message
            self.finished_at = time.time()
            self.events.append({"type": "error", "message": message})

    @property
    def running(self) -> bool:
        return self.status == "running"

    def snapshot(self, cursor: int) -> Tuple[List[Dict[str, Any]], int, bool]:
        """返回 (游标之后的新事件, 新游标, 是否已结束)。"""
        with self._lock:
            events = self.events[cursor:]
            new_cursor = len(self.events)
            done = self.status != "running"
        return events, new_cursor, done

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "log_line_count": len(self.lines),
        }


class JobManager:
    """内存中的任务表（进程重启后运行中的任务即失效，历史消息仍在磁盘上）。"""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str) -> Job:
        job = Job(f"job-{secrets.token_hex(6)}", session_id)
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_session(self, session_id: str) -> Optional[Job]:
        with self._lock:
            for job in self._jobs.values():
                if job.session_id == session_id and job.running:
                    return job
        return None

    def forget_session(self, session_id: str) -> None:
        with self._lock:
            for job_id in [jid for jid, job in self._jobs.items() if job.session_id == session_id]:
                self._jobs.pop(job_id, None)

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if not job.running and job.finished_at and now - job.finished_at > _JOB_TTL_SECONDS
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)
        if len(self._jobs) <= _MAX_KEPT_JOBS:
            return
        finished = sorted(
            (job for job in self._jobs.values() if not job.running),
            key=lambda job: job.finished_at or 0,
        )
        for job in finished[: len(self._jobs) - _MAX_KEPT_JOBS]:
            self._jobs.pop(job.id, None)


__all__ = ["Job", "JobManager", "install_stdout_router", "bind_sink", "unbind_sink"]
