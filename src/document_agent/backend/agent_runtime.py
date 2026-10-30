"""智能体运行环境：知识库懒加载、会话级 prompt 记忆与一次写作任务的执行。

控制台程序一次只跑一个任务，Web 版需要同时支持多个会话，因此：

* 每个会话一个 ``AgentCore``（内含独立的 LangGraph 检查点，保证多轮对话记忆互不干扰）；
* 同一会话同一时刻只允许一个任务（由 ``session_lock`` 保证）；
* Memgraph / 知识图谱在后台线程中初始化，失败时降级为「知识库检索无结果」，
  而不是让整个 Web 服务起不来；初始化状态通过 ``/api/health`` 暴露给前端。
"""

from __future__ import annotations

import importlib
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .jobs import Job, bind_sink, unbind_sink
from .store import SessionStore


# --------------------------------------------------------------------------- 知识库
class _GraphDatabaseProxy:
    """Memgraph 篇章树访问代理：未就绪或查询异常时返回空结果。"""

    def __init__(self, runtime: "KnowledgeRuntime"):
        self._runtime = runtime

    def search_by_vector(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        gdb = self._runtime.graph_instance(timeout=30)
        if gdb is None:
            return []
        try:
            return gdb.search_by_vector(*args, **kwargs)
        except Exception as exc:
            print(f"[知识库] 向量检索失败，本次跳过：{exc}")
            return []

    def get_neighbors(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        gdb = self._runtime.graph_instance(timeout=30)
        if gdb is None:
            return []
        try:
            return gdb.get_neighbors(*args, **kwargs)
        except Exception as exc:
            print(f"[知识库] 邻居节点查询失败，本次跳过：{exc}")
            return []

    def close(self) -> None:
        gdb = self._runtime.graph_instance(timeout=0)
        if gdb is not None:
            try:
                gdb.close()
            except Exception:
                pass


class _KnowledgeGraphProxy:
    """知识图谱记忆代理：未就绪时返回空结果。"""

    def __init__(self, runtime: "KnowledgeRuntime"):
        self._runtime = runtime

    def query(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kg = self._runtime.kg_instance(timeout=30)
        if kg is None:
            return {}
        try:
            result = kg.query(*args, **kwargs)
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            print(f"[知识库] 知识图谱查询失败，本次跳过：{exc}")
            return {}

    def close(self) -> None:
        kg = self._runtime.kg_instance(timeout=0)
        if kg is not None:
            try:
                kg.close()
            except Exception:
                pass


class KnowledgeRuntime:
    """在后台加载 Memgraph 连接与知识图谱记忆，并对外提供代理对象。"""

    def __init__(self) -> None:
        self._settings = config.load_settings()
        self._state = "pending"
        self._message = "知识库尚未初始化"
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._gdb: Optional[Any] = None
        self._kg: Optional[Any] = None
        self.gdb = _GraphDatabaseProxy(self)
        self.kg = _KnowledgeGraphProxy(self)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            thread = threading.Thread(target=self._initialize, name="knowledge-init", daemon=True)
            thread.start()

    def _initialize(self) -> None:
        self._state = "initializing"
        self._message = "正在连接知识库…"
        settings = self._settings
        uri = str(settings.get("gdb_uri") or settings.get("neo4j_uri") or "").strip()
        if not uri:
            self._state = "unavailable"
            self._message = "未配置知识库连接（setting.json 缺少 gdb_uri），知识库检索不可用"
            self._ready.set()
            return
        try:
            from document_agent.memory.neo_graph_db import MemgraphNodeManager

            model_name = settings.get("embedding_model") or "paraphrase-multilingual-MiniLM-L12-v2"
            self._gdb = MemgraphNodeManager(
                uri=uri,
                auth=(settings.get("neo4j_user", ""), settings.get("neo4j_password", "")),
                model_name=model_name,
                database=settings.get("neo4j_database") or "neo4j",
            )
        except Exception as exc:
            self._state = "unavailable"
            self._message = f"篇章树知识库初始化失败：{exc}"
            self._ready.set()
            print(f"[知识库] {self._message}")
            return

        kg_error = ""
        try:
            from document_agent.memory.knowledge_graph import KnowledgeGraphMemory

            self._kg = KnowledgeGraphMemory(config_path=str(config.SETTING_PATH))
        except Exception as exc:
            self._kg = None
            kg_error = str(exc)
            print(f"[知识库] 知识图谱记忆未就绪（仅使用篇章树运行）：{exc}")

        if self._kg is None:
            self._state = "degraded"
            self._message = f"篇章树已就绪，知识图谱不可用：{kg_error}" if kg_error else "篇章树已就绪，知识图谱不可用"
        else:
            self._state = "ready"
            self._message = "知识库已就绪"
        self._ready.set()
        print(f"[知识库] {self._message}")

    # ------------------------------------------------------------------ 访问器
    def graph_instance(self, timeout: float = 0) -> Optional[Any]:
        if not self._started:
            self.start()
        if timeout > 0:
            self._ready.wait(timeout)
        return self._gdb

    def kg_instance(self, timeout: float = 0) -> Optional[Any]:
        if not self._started:
            self.start()
        if timeout > 0:
            self._ready.wait(timeout)
        return self._kg

    def status(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "message": self._message,
            "ready": self._state in ("ready", "degraded"),
        }

    def close(self) -> None:
        for client in (self._gdb, self._kg):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._gdb = None
        self._kg = None


# --------------------------------------------------------------------------- 生成运行时
class GenerationRuntime:
    """会话级的智能体实例与任务执行器。"""

    def __init__(self, store: SessionStore):
        self.store = store
        self.knowledge = KnowledgeRuntime()
        self._cores: Dict[str, Any] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._warmup_started = False

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._guard:
            if self._warmup_started:
                return
            self._warmup_started = True
        threading.Thread(target=self._warmup_agent, name="agent-warmup", daemon=True).start()
        self.knowledge.start()

    @staticmethod
    def _warmup_agent() -> None:
        """后台预导入智能体依赖。

        首次导入 langgraph/langchain 在本机约需 20 秒，提前加载可以让用户第一次
        提交需求时少等这段时间；导入失败也不影响服务启动，真正执行时会再次导入并报错。
        """
        started = time.time()
        try:
            from document_agent.agent import agent_core  # noqa: F401

            for module_name in (
                "intent_node",
                "summary_node",
                "retrieve_node",
                "docx_node",
                "rewrite_node",
                "file_retrieve_node",
                "gdb_retrieve_node",
            ):
                importlib.import_module(module_name)
            print(f"[预热] 写作智能体依赖已加载（{time.time() - started:.1f}s）")
        except Exception as exc:
            print(f"[预热] 写作智能体依赖加载失败（首次生成时会重试）：{exc}")

    def close(self) -> None:
        self.knowledge.close()
        with self._guard:
            self._cores.clear()
            self._locks.clear()

    def health(self) -> Dict[str, Any]:
        knowledge = self.knowledge.status()
        llm_ready = config.llm_configured()
        message = "服务已就绪" if llm_ready else "未配置大模型 API Key（MODEL_API_KEY），无法执行写作任务"
        return {
            "status": "ok" if llm_ready else "degraded",
            "message": message,
            "llm_configured": llm_ready,
            "llm_model": config.llm_model_name(),
            "llm_base_url": config.llm_base_url(),
            "knowledge": knowledge,
            "workspace": str(self.store.root),
        }

    # ------------------------------------------------------------------ 会话锁
    def session_lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def try_begin(self, session_id: str) -> bool:
        """尝试占用会话；已有任务在跑时返回 False。"""
        return self.session_lock(session_id).acquire(blocking=False)

    def forget(self, session_id: str) -> None:
        with self._guard:
            self._cores.pop(session_id, None)
            self._locks.pop(session_id, None)

    # ------------------------------------------------------------------ 智能体实例
    def get_core(self, session_id: str):
        with self._guard:
            core = self._cores.get(session_id)
        if core is not None:
            return core
        if not config.llm_configured():
            raise RuntimeError(
                "未配置大模型 API Key：请先设置环境变量 MODEL_API_KEY（可选 MODEL_BASE_URL、MODEL_NAME）后重启服务"
            )
        from document_agent.agent import agent_core as agent_core_module

        core = agent_core_module.AgentCore()
        core.build_graph(self.knowledge.gdb, self.knowledge.kg)
        with self._guard:
            existing = self._cores.get(session_id)
            if existing is not None:
                return existing
            self._cores[session_id] = core
        return core

    # ------------------------------------------------------------------ 执行任务
    def run_generation(
        self,
        job: Job,
        session_id: str,
        prompt: str,
        assistant_message_id: str,
    ) -> None:
        """执行一次生成任务（在工作线程中运行），结束后释放会话锁。"""
        bind_sink(job.append_log)
        lock = self.session_lock(session_id)
        try:
            session_dir = self.store.session_dir(session_id)
            files = self.store.file_paths(session_id)
            if files:
                job.emit({"type": "status", "message": f"检测到 {len(files)} 个可用文件，开始分析写作需求…"})
            else:
                job.emit({"type": "status", "message": "未检测到参考文件，将直接开始撰写…"})

            job.emit({"type": "status", "message": "正在加载写作智能体…"})
            core = self.get_core(session_id)
            result = core.invoke(
                prompt,
                files,
                session_id=session_id,
                output_dir=str(session_dir),
            )
            self._handle_result(job, session_id, assistant_message_id, result)
        except Exception as exc:  # 任何异常都要落到任务事件里，前端才能看到原因
            traceback.print_exc()
            self._fail(job, session_id, assistant_message_id, self._humanize_error(exc))
        finally:
            try:
                log_name = self.store.save_job_log(session_id, job.id, job.lines)
                if log_name:
                    self.store.update_message(session_id, assistant_message_id, log_file=log_name)
            except Exception as exc:
                print(f"[日志] 保存运行日志失败：{exc}")
            unbind_sink()
            try:
                lock.release()
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ 结果处理
    def _handle_result(
        self,
        job: Job,
        session_id: str,
        assistant_message_id: str,
        result: Optional[Dict[str, Any]],
    ) -> None:
        result = result or {}
        state = result.get("state")
        save_path = str(result.get("save_path") or "").strip()

        if state == "succeeded" and save_path:
            produced = self._ensure_in_session(session_id, save_path)
            if produced is None:
                self._fail(job, session_id, assistant_message_id, f"文档已生成，但输出文件不存在：{save_path}")
                return
            self.store.mark_generated(session_id, [produced.name])
            content = f"文档已生成：{produced.name}"
            self.store.update_message(
                session_id,
                assistant_message_id,
                status="succeeded",
                content=content,
                files=[produced.name],
                save_path=str(produced),
                error=None,
            )
            job.finish(
                {
                    "state": "succeeded",
                    "message": content,
                    "file": produced.name,
                    "save_path": str(produced),
                }
            )
            return

        if state == "succeeded" and not save_path:
            self._fail(job, session_id, assistant_message_id, "任务已结束，但没有得到输出文档路径")
            return

        error = str(result.get("error") or "").strip() or f"生成失败（状态：{state or '未知'}）"
        self._fail(job, session_id, assistant_message_id, error)

    def _ensure_in_session(self, session_id: str, save_path: str) -> Optional[Path]:
        """确保生成结果落在会话目录内：正常情况原样返回，否则兜底复制进来。"""
        try:
            produced = Path(save_path).resolve()
        except OSError:
            return None
        session_dir = self.store.session_dir(session_id).resolve()
        if produced.parent == session_dir and produced.is_file():
            return produced
        if not produced.is_file():
            return None
        target = session_dir / self.store.unique_name(session_id, produced.name)
        shutil.copy2(produced, target)
        print(f"[输出] 生成结果不在会话目录内，已复制到：{target}")
        return target

    def _fail(self, job: Job, session_id: str, assistant_message_id: str, message: str) -> None:
        text = message or "生成失败"
        try:
            self.store.update_message(
                session_id,
                assistant_message_id,
                status="failed",
                error=text,
                content="",
            )
        except Exception as exc:
            print(f"[会话] 更新消息状态失败：{exc}")
        job.fail(text)

    @staticmethod
    def _humanize_error(exc: Exception) -> str:
        text = str(exc).strip()
        if isinstance(exc, ValueError) and ("MODEL_API_KEY" in text or "API key" in text):
            return "未配置大模型 API Key：请设置环境变量 MODEL_API_KEY 后重启服务"
        return text or exc.__class__.__name__


__all__ = ["GenerationRuntime", "KnowledgeRuntime"]
