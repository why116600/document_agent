"""支持知识图谱的 Neo4j 记忆模块。

对外只暴露两个核心接口：

    - :meth:`KnowledgeGraphMemory.add_text`    —— 输入字符串，抽取实体/关系并写入知识图谱；
    - :meth:`KnowledgeGraphMemory.query`       —— 输入提示词，从知识图谱中查询并回答。

图谱结构与配置
--------------
节点：
    ``(:KGEntity)``  图谱实体，属性 ``id / name / type / description / embedding / source_file``
    ``(:KGDocument)`` 原始文档，属性 ``id / content / created_at``

关系：
    ``(:KGEntity)-[:RELATED {relation, description}]->(:KGEntity)``  实体间关系
    ``(:KGDocument)-[:MENTIONS {count}]->(:KGEntity)``               文档提到的实体

Neo4j 连接参数从 ``src/setting.json`` 读取（键名见 :data:`DEFAULT_CONFIG_PATH` 所在文件），
同时兼容环境变量 ``NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE``。

依赖：``neo4j``、``sentence-transformers``（均需安装在 conda 的 ``llm`` 环境中）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# 依赖导入：neo4j 驱动 / 向量模型
# ---------------------------------------------------------------------------
try:
    from neo4j import GraphDatabase
except ImportError as exc:  # pragma: no cover - 环境缺失时给出明确提示
    raise ImportError(
        "缺少 neo4j 驱动，请先安装：\n"
        '  C:\\Users\\wangh\\anaconda3\\envs\\llm\\python.exe -m pip install "neo4j==6.3.0"'
    ) from exc

# 让 ``from memory.knowledge_graph import KnowledgeGraphMemory`` 与包内导入都能工作
_AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.append(str(_AGENT_DIR))

try:
    from llm_client import llm_invoke, llm_model_invoke, extract_json_from_text
except ImportError:  # pragma: no cover
    try:
        from document_agent.agent.llm_client import (  # type: ignore
            llm_invoke,
            llm_model_invoke,
            extract_json_from_text,
        )
    except ImportError:
        llm_invoke = None  # type: ignore
        llm_model_invoke = None  # type: ignore
        extract_json_from_text = None  # type: ignore


# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "setting.json"

ENTITY_LABEL = "KGEntity"
DOCUMENT_LABEL = "KGDocument"
DOC_MENTION_REL = "MENTIONS"
RELATED_REL = "RELATED"

#: 实体名称最大长度，避免超长名称污染图谱
MAX_NAME_LEN = 120
#: 一次 ``add_text`` 最多写入的实体/关系条数，防止大模型返回失控内容
MAX_ENTITIES_PER_TEXT = 200
MAX_RELATIONS_PER_TEXT = 400

# 关系类型只允许字母/数字/下划线/汉字，避免 Cypher 注入
_REL_TYPE_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]")
# 实体类型归一化
_TYPE_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]")

#: 全文兜底配置（setting.json 缺失时使用）
FALLBACK_CONFIG: Dict[str, Any] = {
    "neo4j_uri": "bolt://localhost:7687",
    "neo4j_user": "neo4j",
    "neo4j_password": "",
    "neo4j_database": "neo4j",
    "embedding_model": "all-MiniLM-L6-v2",
    "hf_endpoint": "https://hf-mirror.com",
    "hf_home": "",
    "entity_vector_index": "kg_entity_embedding",
    "document_vector_index": "kg_document_embedding",
    "top_k": 8,
    "neighbor_limit": 8,
    "enable_vector_index": True,
    "require_llm": True,
}


def load_settings(config_path: Optional[Any] = None) -> Dict[str, Any]:
    """读取 ``src/setting.json``。

    :param config_path: 配置文件路径；为 ``None`` 时使用 :data:`DEFAULT_CONFIG_PATH`。
    :return: 配置字典，缺失的键用 :data:`FALLBACK_CONFIG` 补齐。
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    config: Dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fp:
            config = json.loads(fp.read() or "{}")
        if not isinstance(config, dict):
            raise ValueError(f"配置文件格式错误（应为 JSON 对象）：{path}")

    cfg = dict(FALLBACK_CONFIG)
    # 兼容项目里已有的 gdb_uri
    if config.get("gdb_uri") and not config.get("neo4j_uri"):
        config["neo4j_uri"] = config["gdb_uri"]
    cfg.update({k: v for k, v in config.items() if v is not None})
    cfg["_config_path"] = str(path)

    # 环境变量优先级最高，便于不改文件切换环境
    for key, env in (
        ("neo4j_uri", "NEO4J_URI"),
        ("neo4j_user", "NEO4J_USER"),
        ("neo4j_password", "NEO4J_PASSWORD"),
        ("neo4j_database", "NEO4J_DATABASE"),
    ):
        if os.getenv(env):
            cfg[key] = os.getenv(env)
    return cfg


def _sanitize_rel_type(relation: Optional[str]) -> str:
    """把大模型给出的关系名整理成合法的 Cypher 关系类型。"""
    if not relation:
        return RELATED_REL
    cleaned = _REL_TYPE_SAFE.sub("_", str(relation)).strip("_").upper()
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if not cleaned:
        return RELATED_REL
    first = cleaned[0]
    # Cypher 关系类型不能以数字开头
    if not (first.isalpha() or first == "_" or "\u4e00" <= first <= "\u9fff"):
        cleaned = f"R_{cleaned}"
    return cleaned[:64]


def _sanitize_entity_type(entity_type: Optional[str]) -> str:
    """实体类型仅作为属性保存，这里做最小化清洗。"""
    if not entity_type:
        return "未分类"
    cleaned = _TYPE_SAFE.sub("", str(entity_type)).strip()
    return (cleaned or "未分类")[:40]


def _norm_name(name: str) -> str:
    """实体名称归一化，用于精确去重。"""
    return re.sub(r"\s+", " ", str(name)).strip().lower()


class KnowledgeGraphMemory:
    """基于 Neo4j 的知识图谱记忆。

    典型用法::

        from memory.knowledge_graph import KnowledgeGraphMemory

        kg = KnowledgeGraphMemory()                 # 自动读取 src/setting.json
        kg.add_text("张三于 2024 年加入示例科技有限公司，担任技术总监。")
        print(kg.query("张三在哪个公司担任什么职务？"))
        kg.close()

    图谱写入与查询都会调用大模型（抽取 / 归纳），大模型配置沿用项目里
    ``agent/llm_client.py`` 的方式：环境变量 ``MODEL_API_KEY``、``MODEL_BASE_URL``、
    ``MODEL_NAME``。
    """

    def __init__(
        self,
        config_path: Optional[Any] = None,
        *,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        embedding_model: Optional[str] = None,
        llm: Any = None,
        require_llm: Optional[bool] = None,
        auto_init: bool = True,
        **overrides: Any,
    ) -> None:
        """
        :param config_path: ``setting.json`` 路径，默认 ``src/setting.json``。
        :param uri: 覆盖 bolt 连接串，如 ``bolt://localhost:7687``。
        :param user: 覆盖 Neo4j 用户名。
        :param password: 覆盖 Neo4j 密码。
        :param database: 覆盖数据库名，默认 ``neo4j``。
        :param embedding_model: 覆盖句向量模型名。
        :param llm: 直接注入一个 LangChain Chat 模型；为 ``None`` 时自动构建。
        :param require_llm: 为 ``True`` 时缺少大模型配置会直接报错；默认取配置文件里的设置。
        :param auto_init: 是否在构造时连接数据库并创建索引/约束。
        :param overrides: 其它配置项覆盖，如 ``top_k=5``、``enable_vector_index=False``。
        """
        cfg = load_settings(config_path)
        cfg.update({k: v for k, v in overrides.items() if v is not None})

        self.config: Dict[str, Any] = cfg
        self.uri: str = uri or str(cfg["neo4j_uri"])
        self.user: str = str(user if user is not None else cfg.get("neo4j_user", "neo4j"))
        self.password: str = str(
            password if password is not None else cfg.get("neo4j_password", "")
        )
        self.database: str = str(
            database or cfg.get("neo4j_database") or "neo4j"
        )
        self.embedding_model_name: str = str(
            embedding_model or cfg["embedding_model"]
        )
        self.hf_endpoint: str = str(cfg.get("hf_endpoint") or "")
        self.hf_home: str = str(cfg.get("hf_home") or "")
        self.entity_index: str = str(cfg["entity_vector_index"])
        self.document_index: str = str(cfg["document_vector_index"])
        self.default_top_k: int = int(cfg.get("top_k", 8))
        self.neighbor_limit: int = int(cfg.get("neighbor_limit", 8))
        self.enable_vector_index: bool = bool(cfg.get("enable_vector_index", True))
        self._require_llm: bool = (
            bool(cfg.get("require_llm", True)) if require_llm is None else bool(require_llm)
        )

        # 组装鉴权：
        #   - 没有密码 => 视为无鉴权（Neo4j 默认关闭认证时传空密码会报 Unsupported authentication token）
        #   - 有密码   => 使用 (user, password)
        self._auth: Optional[Tuple[str, str]] = (
            (self.user, self.password) if self.password else None
        )

        # 延迟加载句向量模型（首次使用时才载入，避免构造慢）
        self._embedder: Any = None
        self._vector_dim: Optional[int] = None

        # 大模型
        self._llm = llm
        self._llm_ready: Optional[bool] = None

        # 驱动与索引状态
        self._driver: Any = None
        self._indexes_ready = False
        self._backend: str = "neo4j"

        if auto_init:
            self._ensure_driver()
            self.init_schema()

    # =====================================================================
    # 内部：驱动 / 索引 / 模型
    # =====================================================================
    def _ensure_driver(self) -> Any:
        """惰性创建 neo4j 驱动。

        若配置了密码但认证失败，会再尝试一次无鉴权连接（便于本地未开启认证的实例）。
        """
        if self._driver is None:
            driver = GraphDatabase.driver(self.uri, auth=self._auth)
            try:
                driver.verify_connectivity()
            except Exception as exc:
                if self._auth is not None:
                    driver.close()
                    print(
                        f"[KnowledgeGraphMemory] 使用账号 {self.user!r} 连接失败，"
                        f"改为无鉴权连接重试：{exc}"
                    )
                    fallback = GraphDatabase.driver(self.uri, auth=None)
                    fallback.verify_connectivity()
                    self._driver = fallback
                    self._auth = None
                    return self._driver
                raise
            self._driver = driver
        return self._driver

    def _session(self, **kwargs: Any):
        """创建会话。

        关闭通知日志：Neo4j 会把 ``CALL`` 子查询写法、属性索引等提示直接打到 stderr，
        对使用者是噪声（这些写法在 5.x/6.x 上都能正常工作）。
        """
        try:
            return self._ensure_driver().session(
                database=self.database, notifications_min_severity="OFF", **kwargs
            )
        except Exception:
            # 个别后端（如 Memgraph）不认这个参数，退回默认会话
            return self._ensure_driver().session(database=self.database, **kwargs)

    def _detect_backend(self) -> str:
        """区分 Neo4j 与 Memgraph，两者的向量索引语法不同。"""
        try:
            with self._session() as session:
                record = session.run(
                    "CALL dbms.components() YIELD name RETURN name"
                ).single()
                name = (record["name"] if record else "") or ""
            self._backend = "memgraph" if "memgraph" in name.lower() else "neo4j"
        except Exception:
            self._backend = "neo4j"
        return self._backend

    def init_schema(self) -> None:
        """创建唯一约束与向量索引（幂等，可重复调用）。"""
        self._ensure_driver()
        self._detect_backend()

        statements = [
            f"CREATE CONSTRAINT kg_entity_id IF NOT EXISTS "
            f"FOR (n:{ENTITY_LABEL}) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT kg_document_id IF NOT EXISTS "
            f"FOR (n:{DOCUMENT_LABEL}) REQUIRE n.id IS UNIQUE",
            f"CREATE INDEX kg_entity_name IF NOT EXISTS "
            f"FOR (n:{ENTITY_LABEL}) ON (n.name)",
        ]
        with self._session() as session:
            for stmt in statements:
                try:
                    session.run(stmt).consume()
                except Exception as exc:  # 约束已存在或后端不支持时不阻塞
                    print(f"[KnowledgeGraphMemory] 初始化语句跳过：{exc}")

            if self.enable_vector_index:
                dim = self._embedding_dim()
                self._create_vector_index(session, self.entity_index, ENTITY_LABEL, dim)
                self._create_vector_index(session, self.document_index, DOCUMENT_LABEL, dim)

        self._indexes_ready = True

    def _create_vector_index(self, session: Any, index_name: str, label: str, dim: int) -> None:
        """按后端创建向量索引。"""
        try:
            if self._backend == "memgraph":
                session.run(
                    f'CREATE VECTOR INDEX {index_name} ON :{label}(embedding) '
                    f'WITH CONFIG {{"dimension": {dim}, "capacity": 100000, "metric": "cos"}}'
                ).consume()
            else:
                session.run(
                    f"CREATE VECTOR INDEX {index_name} IF NOT EXISTS "
                    f"FOR (n:{label}) ON (n.embedding) "
                    f"OPTIONS {{ indexConfig: {{ "
                    f"`vector.dimensions`: {dim}, "
                    f"`vector.similarity_function`: 'cosine' }} }}"
                ).consume()
        except Exception as exc:
            # 例如维度与已有索引不一致，此时退化为本地向量计算，不影响功能
            print(f"[KnowledgeGraphMemory] 向量索引 {index_name} 创建跳过：{exc}")

    # ------------------------------------------------------------------
    # 向量
    # ------------------------------------------------------------------
    def _resolve_model_cache_dir(self) -> str:
        """确定模型缓存目录；返回空串表示用库默认位置。

        优先级：``hf_home`` 配置 > 环境变量 HF_HOME > 项目内 ``.models``（默认，自动创建）。
        """
        explicit = self.hf_home or os.getenv("HF_HOME")
        path = Path(explicit) if explicit else DEFAULT_CONFIG_PATH.parent.parent / ".models"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"[KnowledgeGraphMemory] 缓存目录 {path} 不可用，改用库默认位置：{exc}")
            return ""
        return str(path)

    def _resolve_model_source(self, cache_dir: str) -> str:
        """把 ``embedding_model`` 解析成可直接加载的模型来源。

        支持两种写法：

        1. **本地目录**：绝对路径，或 ``.models`` 下同名子目录（推荐，离线可用）；
        2. **模型名**（如 ``all-MiniLM-L6-v2``）：需要联网从 HF/镜像下载。
        """
        raw = self.embedding_model_name
        raw_path = Path(raw)
        if raw_path.exists():
            return str(raw_path)
        if cache_dir:
            local = Path(cache_dir) / raw_path.name
            if local.is_dir() and any(local.iterdir()):
                return str(local)
        return raw

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "缺少 sentence-transformers，请先安装：\n"
                    "  conda run -n llm python -m pip install sentence-transformers"
                ) from exc
            # 国内网络访问 huggingface.co 常常超时，按配置切换到镜像站
            if self.hf_endpoint and not os.getenv("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = self.hf_endpoint
                print(
                    f"[KnowledgeGraphMemory] 使用 HF 镜像：{self.hf_endpoint}"
                    "（如需还原，删除 setting.json 中的 hf_endpoint）"
                )
            cache_dir = self._resolve_model_cache_dir()
            if cache_dir:
                os.environ.setdefault("HF_HOME", cache_dir)
            source = self._resolve_model_source(cache_dir)
            is_local = Path(source).is_dir()

            print(
                f"[KnowledgeGraphMemory] 加载句向量模型：{source}"
                f"{'（本地目录）' if is_local else '（在线下载）'}"
            )
            try:
                self._embedder = SentenceTransformer(
                    source,
                    cache_folder=None if is_local else (cache_dir or None),
                    local_files_only=is_local,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"加载句向量模型 {self.embedding_model_name} 失败：{exc}\n"
                    "排查建议：\n"
                    "  1) 网络问题：在 setting.json 配置 hf_endpoint（如 https://hf-mirror.com）；\n"
                    "  2) 缓存目录不可写：在 setting.json 配置 hf_home 指向可写目录；\n"
                    "  3) 离线使用：把模型文件放到 "
                    f"{(cache_dir or '.models')}\\{Path(self.embedding_model_name).name} 目录下，\n"
                    "     或把 embedding_model 直接写成模型目录的绝对路径。"
                ) from exc
        return self._embedder

    def _embedding_dim(self) -> int:
        if self._vector_dim is None:
            model = self._get_embedder()
            dim = getattr(model, "get_embedding_dimension", None)
            if callable(dim):
                self._vector_dim = int(dim())
            else:
                self._vector_dim = int(model.get_sentence_embedding_dimension())
        return self._vector_dim

    def embed(self, text: str) -> List[float]:
        """文本 -> 归一化向量（列表）。"""
        model = self._get_embedder()
        vector = model.encode(str(text), normalize_embeddings=True)
        if self._vector_dim is None:
            self._vector_dim = int(len(vector))
        return np.asarray(vector, dtype=np.float32).tolist()

    @staticmethod
    def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
        va = np.asarray(list(a), dtype=np.float32)
        vb = np.asarray(list(b), dtype=np.float32)
        denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
        if denom == 0.0:
            return 0.0
        return float(np.dot(va, vb) / denom)

    # ------------------------------------------------------------------
    # 大模型
    # ------------------------------------------------------------------
    def _get_llm(self) -> Any:
        """获取大模型；不可用时返回 ``None``。"""
        if self._llm is not None:
            return self._llm
        if self._llm_ready is False:
            return None
        if llm_invoke is None:
            self._llm_ready = False
            return None
        try:
            _AGENT_DIR_STR = str(_AGENT_DIR)
            if _AGENT_DIR_STR not in sys.path:
                sys.path.append(_AGENT_DIR_STR)
            from llm_client import get_deepseek_llm  # type: ignore

            self._llm = get_deepseek_llm()
            self._llm_ready = True
        except Exception as exc:
            self._llm_ready = False
            if self._require_llm:
                raise RuntimeError(
                    "无法初始化大模型，请设置环境变量 MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME；"
                    "或传入 llm 参数、把 setting.json 的 require_llm 设为 false。"
                    f"原始错误：{exc}"
                ) from exc
            print(f"[KnowledgeGraphMemory] 未启用大模型：{exc}")
        return self._llm

    def _invoke_llm(self, prompt: str) -> str:
        """调用大模型并返回文本内容，失败返回空串。"""
        llm = self._get_llm()
        if llm is None:
            return ""
        result = llm_invoke(llm, prompt, retry=2)
        return str(getattr(result, "content", "") or "")

    # =====================================================================
    # 接口一：写入字符串
    # =====================================================================
    def add_text(
        self,
        text: str,
        *,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        extract_relations: bool = True,
    ) -> Dict[str, Any]:
        """把一段字符串写入知识图谱。

        流程：大模型抽取实体与关系 -> 实体按名称/向量去重合并 -> 写入 Neo4j。

        :param text: 待录入的原始文本。
        :param source: 来源标识（文件名、URL 等），用于溯源。
        :param metadata: 附加到文档节点上的自定义属性（值需为 Neo4j 支持的基础类型）。
        :param extract_relations: 是否抽取实体间关系，``False`` 时只登记实体与文档。
        :return: 统计信息 ``{"document_id", "entities", "relations", "chunks"}``。
        """
        if not text or not str(text).strip():
            raise ValueError("add_text 的输入文本不能为空")

        if not self._indexes_ready:
            self.init_schema()

        text = str(text)
        chunks = self._split_text(text)
        doc_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        # 1) 抽取
        extraction: Dict[str, List[Dict[str, Any]]] = {"entities": [], "relations": []}
        for chunk in chunks:
            part = self._extract_graph(chunk, extract_relations=extract_relations)
            extraction["entities"].extend(part["entities"])
            extraction["relations"].extend(part["relations"])

        entities = self._dedupe_entities(extraction["entities"])
        relations = extraction["relations"]

        # 2) 写库
        doc_props: Dict[str, Any] = {
            "id": doc_id,
            "content": text,
            "source_file": str(source) if source else "",
            "created_at": created_at,
        }
        for key, value in (metadata or {}).items():
            if value is not None and not key.startswith("_"):
                doc_props[str(key)] = value

        written_entities: List[Dict[str, Any]] = []
        written_relations = 0

        try:
            with self._session() as session:
                doc_props["embedding"] = self.embed(text[:4000])
                session.run(
                    f"CREATE (d:{DOCUMENT_LABEL}) SET d = $props",
                    props=doc_props,
                ).consume()

                for entity in entities:
                    node = self._upsert_entity(
                        session, entity, doc_props["source_file"], created_at, doc_id
                    )
                    if node:
                        written_entities.append(node)

                if extract_relations and relations:
                    written_relations = self._write_relations(
                        session, written_entities, relations
                    )
        except Exception as exc:
            # 连接/写入失败时给出明确提示，避免静默丢数据
            raise RuntimeError(f"知识图谱写入失败：{exc}") from exc

        result = {
            "document_id": doc_id,
            "entities": written_entities,
            "relations": written_relations,
            "chunks": len(chunks),
            "source": doc_props["source_file"],
        }
        print(
            f"[KnowledgeGraphMemory] 已录入文档 {doc_id}："
            f"实体 {len(written_entities)} 个，关系 {written_relations} 条，文本分块 {len(chunks)}"
        )
        return result

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------
    def _split_text(self, text: str, max_chars: int = 2000, overlap: int = 200) -> List[str]:
        """按段落聚合切块，过长时按字符硬切，块间保留少量重叠。"""
        text = text.strip()
        if len(text) <= max_chars:
            return [text]

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks: List[str] = []
        buffer = ""
        for para in paragraphs:
            if len(para) > max_chars:
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                start = 0
                while start < len(para):
                    chunks.append(para[start : start + max_chars])
                    start += max_chars - overlap
                continue
            if len(buffer) + len(para) + 1 <= max_chars:
                buffer = f"{buffer}\n{para}" if buffer else para
            else:
                chunks.append(buffer)
                buffer = para
        if buffer:
            chunks.append(buffer)
        return chunks or [text]

    _EXTRACT_PROMPT = (
        "你是一个知识图谱构建助手。请从下面的文本中抽取实体和实体之间的关系。\n"
        "要求：\n"
        "1. 实体名称要具体、简洁，不要出现代词（如“他”“该公司”）；\n"
        "2. 实体类型用简短中文分类，例如：人物、组织、地点、时间、事件、产品、概念；\n"
        "3. 关系的名称用简短的中文动词短语，例如：任职于、属于、位于、成立于、负责；\n"
        "4. 只输出 JSON，不要输出任何解释文字，格式如下：\n"
        '{{"entities": [{{"name": "实体名", "type": "实体类型", "description": "对该实体的一句话说明"}}], '
        '"relations": [{{"source": "头实体名", "relation": "关系名", "target": "尾实体名", '
        '"description": "对该关系的一句话说明"}}]}}\n'
        "5. 如果文本中没有可抽取的内容，返回 {{\"entities\": [], \"relations\": []}}。\n\n"
        "文本：\n{text}\n"
    )

    def _extract_graph(self, chunk: str, extract_relations: bool = True) -> Dict[str, List[Dict[str, Any]]]:
        """对大模型输出做解析；大模型不可用时退化为规则抽取。"""
        raw = self._invoke_llm(self._EXTRACT_PROMPT.format(text=chunk))
        parsed = extract_json_from_text(raw) if (raw and extract_json_from_text) else None

        if not isinstance(parsed, dict):
            if raw:
                print("[KnowledgeGraphMemory] 抽取结果不是合法 JSON，改用规则抽取")
            return self._heuristic_extract(chunk, extract_relations=extract_relations)

        entities: List[Dict[str, Any]] = []
        for item in parsed.get("entities") or []:
            if isinstance(item, dict) and str(item.get("name", "")).strip():
                entities.append(
                    {
                        "name": str(item["name"]).strip()[:MAX_NAME_LEN],
                        "type": _sanitize_entity_type(item.get("type")),
                        "description": str(item.get("description", "") or "").strip(),
                    }
                )

        relations: List[Dict[str, Any]] = []
        if extract_relations:
            for item in parsed.get("relations") or []:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("source", "") or "").strip()
                tgt = str(item.get("target", "") or "").strip()
                if not src or not tgt:
                    continue
                relations.append(
                    {
                        "source": src[:MAX_NAME_LEN],
                        "target": tgt[:MAX_NAME_LEN],
                        "relation": _sanitize_rel_type(item.get("relation")),
                        "description": str(item.get("description", "") or "").strip(),
                    }
                )
                # 关系两端的实体即使模型没单独列出，也要补进实体表
                for name in (src, tgt):
                    if name and not any(_norm_name(e["name"]) == _norm_name(name) for e in entities):
                        entities.append(
                            {"name": name[:MAX_NAME_LEN], "type": "未分类", "description": ""}
                        )

        return {
            "entities": entities[:MAX_ENTITIES_PER_TEXT],
            "relations": relations[:MAX_RELATIONS_PER_TEXT],
        }

    def _heuristic_extract(self, chunk: str, extract_relations: bool = True) -> Dict[str, List[Dict[str, Any]]]:
        """无大模型时的兜底：以句子为单元抽取名词性短语作为实体。

        该策略只保证接口可用与图谱不空，语义质量明显低于大模型抽取。
        """
        entities: List[Dict[str, Any]] = []
        seen = set()
        # 取中文/英文的连续名词性片段（长度 2~20），并过滤常见停用表达
        candidates = re.findall(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{1,19}", chunk)
        stop = {"我们", "他们", "这个", "那个", "以及", "并且", "但是", "因为", "所以", "如果", "可以"}
        for word in candidates:
            key = _norm_name(word)
            if key in seen or key in stop or len(word) < 2:
                continue
            seen.add(key)
            entities.append({"name": word, "type": "未分类", "description": ""})
            if len(entities) >= 40:
                break
        return {"entities": entities, "relations": []}

    @staticmethod
    def _dedupe_entities(entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在写入前按名称合并重复实体，描述取较长的一条。"""
        merged: Dict[str, Dict[str, Any]] = {}
        for entity in entities:
            key = _norm_name(entity["name"])
            if not key:
                continue
            if key in merged:
                old = merged[key]
                if len(entity.get("description", "")) > len(old.get("description", "")):
                    old["description"] = entity["description"]
                if old.get("type") in ("", "未分类") and entity.get("type"):
                    old["type"] = entity["type"]
            else:
                merged[key] = dict(entity)
        return list(merged.values())

    # ------------------------------------------------------------------
    # 实体 upsert
    # ------------------------------------------------------------------
    def _upsert_entity(
        self,
        session: Any,
        entity: Dict[str, Any],
        source_file: str,
        created_at: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        """按名称精确匹配 -> 向量相似匹配 -> 新建，三级策略合并实体。"""
        name = entity["name"]
        normalized = _norm_name(name)
        description = entity.get("description", "") or ""
        entity_type = entity.get("type") or "未分类"
        # 名称 + 说明一起编码：仅靠名称很难把“示例科技”和“示例科技有限公司”判为同一实体
        vector = self.embed(f"{name} {description}".strip())

        # 1) 精确匹配（忽略大小写与首尾空白）
        record = session.run(
            f"MATCH (e:{ENTITY_LABEL}) WHERE toLower(trim(e.name)) = $normalized "
            f"RETURN e.id AS id LIMIT 1",
            normalized=normalized,
        ).single()

        if record:
            entity_id = record["id"]
            session.run(
                f"MATCH (e:{ENTITY_LABEL} {{id: $id}}) "
                f"SET e.updated_at = $now, "
                f"    e.description = CASE WHEN size(coalesce(e.description,'')) < size($description) "
                f"                         THEN $description ELSE e.description END, "
                f"    e.type = CASE WHEN coalesce(e.type,'') IN ['', '未分类'] THEN $type ELSE e.type END, "
                f"    e.mention_count = coalesce(e.mention_count, 0) + 1",
                id=entity_id,
                description=description,
                type=entity_type,
                now=created_at,
            ).consume()
            self._link_document(session, doc_id, entity_id, created_at)
            return {"id": entity_id, "name": name, "type": entity_type, "merged": True}

        # 2) 向量相似匹配，合并同义表述（如“示例科技有限公司”与“示例科技”）
        similar_id = self._find_similar_entity(session, vector, normalized)
        if similar_id:
            session.run(
                f"MATCH (e:{ENTITY_LABEL} {{id: $id}}) "
                f"SET e.updated_at = $now, "
                f"    e.description = CASE WHEN size(coalesce(e.description,'')) < size($description) "
                f"                         THEN $description ELSE e.description END, "
                f"    e.mention_count = coalesce(e.mention_count, 0) + 1",
                id=similar_id,
                description=description,
                now=created_at,
            ).consume()
            self._link_document(session, doc_id, similar_id, created_at)
            return {"id": similar_id, "name": name, "type": entity_type, "merged": True}

        # 3) 新建实体
        entity_id = str(uuid.uuid4())
        session.run(
            f"CREATE (e:{ENTITY_LABEL}) SET e = $props",
            props={
                "id": entity_id,
                "name": name,
                "normalized_name": normalized,
                "type": entity_type,
                "description": description,
                "embedding": vector,
                "source_file": source_file,
                "created_at": created_at,
                "updated_at": created_at,
                "mention_count": 1,
            },
        ).consume()
        self._link_document(session, doc_id, entity_id, created_at)
        return {"id": entity_id, "name": name, "type": entity_type, "merged": False}

    def _link_document(
        self, session: Any, doc_id: str, entity_id: str, created_at: str
    ) -> None:
        """建立 文档 -[:MENTIONS]-> 实体 的关系。"""
        if not doc_id:
            return
        session.run(
            f"MATCH (d:{DOCUMENT_LABEL} {{id: $doc_id}}), (e:{ENTITY_LABEL} {{id: $entity_id}}) "
            f"MERGE (d)-[m:{DOC_MENTION_REL}]->(e) "
            f"SET m.count = coalesce(m.count, 0) + 1, m.last_seen = $now",
            doc_id=doc_id,
            entity_id=entity_id,
            now=created_at,
        ).consume()

    def _find_similar_entity(
        self, session: Any, vector: List[float], normalized: str, threshold: float = 0.95
    ) -> Optional[str]:
        """用向量索引找语义相同的已有实体；关闭索引时退化为本地计算。"""
        if self.enable_vector_index and self._indexes_ready:
            try:
                record = session.run(
                    f"CALL db.index.vector.queryNodes($index, 1, $vector) "
                    f"YIELD node, score "
                    f"WHERE score >= $threshold AND toLower(trim(node.name)) <> $normalized "
                    f"RETURN node.id AS id",
                    index=self.entity_index,
                    vector=vector,
                    threshold=threshold,
                    normalized=normalized,
                ).single()
                if record:
                    return record["id"]
                return None
            except Exception:
                pass  # 索引不可用时走本地计算

        best_id, best_score = None, threshold
        for row in session.run(
            f"MATCH (e:{ENTITY_LABEL}) WHERE e.embedding IS NOT NULL "
            f"RETURN e.id AS id, e.embedding AS embedding LIMIT 5000"
        ):
            score = self._cosine(vector, row["embedding"])
            if score >= best_score:
                best_id, best_score = row["id"], score
        return best_id

    def _write_relations(
        self,
        session: Any,
        written_entities: List[Dict[str, Any]],
        relations: List[Dict[str, Any]],
    ) -> int:
        """按名称把关系两端映射到已写库的实体 id，然后写入关系。"""
        # 名称 -> 实体 id（含本次写入的实体与图谱中已有的实体）
        name_to_id: Dict[str, str] = {}
        for entity in written_entities:
            name_to_id[_norm_name(entity["name"])] = entity["id"]

        missing = {
            _norm_name(rel[side])
            for rel in relations
            for side in ("source", "target")
            if _norm_name(rel[side]) not in name_to_id
        }
        if missing:
            for row in session.run(
                f"MATCH (e:{ENTITY_LABEL}) WHERE e.normalized_name IN $names "
                f"RETURN e.normalized_name AS name, e.id AS id",
                names=list(missing),
            ):
                name_to_id.setdefault(row["name"], row["id"])

        count = 0
        for rel in relations:
            src_id = name_to_id.get(_norm_name(rel["source"]))
            tgt_id = name_to_id.get(_norm_name(rel["target"]))
            if not src_id or not tgt_id or src_id == tgt_id:
                continue
            rel_type = _sanitize_rel_type(rel.get("relation"))
            session.run(
                f"MATCH (a:{ENTITY_LABEL} {{id: $src}}), (b:{ENTITY_LABEL} {{id: $tgt}}) "
                f"MERGE (a)-[r:`{rel_type}`]->(b) "
                f"SET r.relation = coalesce(r.relation, $relation), "
                f"    r.description = CASE WHEN size(coalesce(r.description,'')) < size($description) "
                f"                          THEN $description ELSE r.description END, "
                f"    r.weight = coalesce(r.weight, 0) + 1, r.updated_at = $now",
                src=src_id,
                tgt=tgt_id,
                relation=rel.get("relation") or rel_type,
                description=rel.get("description", "") or "",
                now=datetime.now(timezone.utc).isoformat(),
            ).consume()
            count += 1
        return count

    # =====================================================================
    # 接口二：按提示词查询
    # =====================================================================
    def query(
        self,
        prompt: str,
        *,
        top_k: Optional[int] = None,
        depth: int = 1,
        include_documents: bool = True,
        return_context: bool = False,
    ) -> Any:
        """输入提示词，从知识图谱中查询内容并返回答案。

        流程：提示词向量检索相关实体 -> 扩展邻居关系与关联原文 -> 大模型归纳作答。

        :param prompt: 自然语言提问。
        :param top_k: 召回实体数量，默认取配置里的 ``top_k``。
        :param depth: 邻居扩展跳数，``0`` 表示只返回命中的实体。
        :param include_documents: 是否把实体关联的原文片段一并作为上下文。
        :param return_context: 为 ``True`` 时返回 ``{"answer", "context", "entities"}`` 字典，
            否则只返回答案字符串。
        :return: 答案字符串；``return_context=True`` 时返回字典。
        """
        if not prompt or not str(prompt).strip():
            raise ValueError("query 的提示词不能为空")

        if not self._indexes_ready:
            self.init_schema()

        prompt = str(prompt)
        k = int(top_k or self.default_top_k)

        with self._session() as session:
            entities = self._search_entities(session, prompt, k)
            if not entities:
                empty = {"answer": "知识图谱中没有找到与该问题相关的内容。", "context": "", "entities": []}
                return empty if return_context else empty["answer"]

            subgraph = self._collect_subgraph(session, entities, depth)
            documents = self._fetch_documents(session, entities) if include_documents else []
            context = self._render_context(subgraph, documents)

        answer = self._answer_with_llm(prompt, context)
        if return_context:
            return {"answer": answer, "context": context, "entities": entities}
        return answer

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def _search_entities(self, session: Any, prompt: str, top_k: int) -> List[Dict[str, Any]]:
        """向量检索实体，索引不可用时退化为全量本地计算。"""
        vector = self.embed(prompt)

        if self.enable_vector_index and self._indexes_ready:
            try:
                records = session.run(
                    f"CALL db.index.vector.queryNodes($index, $k, $vector) "
                    f"YIELD node, score "
                    f"RETURN node.id AS id, node.name AS name, node.type AS type, "
                    f"       node.description AS description, score "
                    f"ORDER BY score DESC",
                    index=self.entity_index,
                    k=max(top_k * 2, top_k),
                    vector=vector,
                )
                items = [dict(record) for record in records]
                if items:
                    return items[:top_k]
            except Exception as exc:
                print(f"[KnowledgeGraphMemory] 向量索引检索不可用，改为本地计算：{exc}")

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in session.run(
            f"MATCH (e:{ENTITY_LABEL}) WHERE e.embedding IS NOT NULL "
            f"RETURN e.id AS id, e.name AS name, e.type AS type, "
            f"       e.description AS description, e.embedding AS embedding"
        ):
            score = self._cosine(vector, row["embedding"])
            scored.append(
                (
                    score,
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "type": row["type"],
                        "description": row["description"],
                        "score": score,
                    },
                )
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    def _collect_subgraph(
        self, session: Any, entities: List[Dict[str, Any]], depth: int
    ) -> Dict[str, Any]:
        """取命中实体的邻居关系，构成子图上下文。"""
        ids = [entity["id"] for entity in entities]
        rels: List[Dict[str, Any]] = []
        neighbors: Dict[str, Dict[str, Any]] = {}

        if depth and depth > 0:
            limit = max(self.neighbor_limit, 1)
            records = session.run(
                f"MATCH (e:{ENTITY_LABEL}) WHERE e.id IN $ids "
                f"MATCH path = (e)-[*1..{int(depth)}]-(m:{ENTITY_LABEL}) "
                f"UNWIND relationships(path) AS r "
                f"WITH DISTINCT r, startNode(r) AS a, endNode(r) AS b "
                f"LIMIT $limit "
                f"RETURN a.id AS a_id, a.name AS a_name, a.type AS a_type, "
                f"       b.id AS b_id, b.name AS b_name, b.type AS b_type, "
                f"       type(r) AS relation, r.description AS description, "
                f"       coalesce(r.weight, 1) AS weight",
                ids=ids,
                limit=limit * max(len(ids), 1),
            )
            for row in records:
                rels.append(dict(row))
                for prefix in ("a", "b"):
                    nid = row[f"{prefix}_id"]
                    if nid not in ids:
                        neighbors[nid] = {
                            "id": nid,
                            "name": row[f"{prefix}_name"],
                            "type": row[f"{prefix}_type"],
                        }

        return {"entities": entities, "relations": rels, "neighbors": list(neighbors.values())}

    def _fetch_documents(self, session: Any, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """取出命中实体关联的原始文本片段，用于补充上下文。"""
        ids = [entity["id"] for entity in entities]
        documents: List[Dict[str, Any]] = []
        for row in session.run(
            f"MATCH (d:{DOCUMENT_LABEL})-[:{DOC_MENTION_REL}]->(e:{ENTITY_LABEL}) "
            f"WHERE e.id IN $ids "
            f"RETURN DISTINCT d.id AS id, d.content AS content, d.source_file AS source_file "
            f"LIMIT $limit",
            ids=ids,
            limit=max(self.neighbor_limit, 1),
        ):
            content = row["content"] or ""
            documents.append(
                {
                    "id": row["id"],
                    "source_file": row["source_file"] or "",
                    "content": content[:800],
                }
            )
        return documents

    @staticmethod
    def _render_context(subgraph: Dict[str, Any], documents: List[Dict[str, Any]]) -> str:
        """把子图渲染成给大模型阅读的纯文本上下文。"""
        lines: List[str] = []
        if subgraph["entities"]:
            lines.append("【相关实体】")
            for entity in subgraph["entities"]:
                desc = f"：{entity['description']}" if entity.get("description") else ""
                score = entity.get("score")
                score_txt = f"（相关度 {score:.2f}）" if isinstance(score, (int, float)) else ""
                lines.append(f"- {entity['name']}（{entity.get('type') or '未分类'}）{desc}{score_txt}")
        if subgraph["relations"]:
            lines.append("【实体间关系】")
            seen = set()
            for rel in subgraph["relations"]:
                key = (rel["a_id"], rel["relation"], rel["b_id"])
                if key in seen:
                    continue
                seen.add(key)
                desc = f"（{rel['description']}）" if rel.get("description") else ""
                lines.append(
                    f"- {rel['a_name']} --[{rel['relation']}]--> {rel['b_name']}{desc}"
                )
        if subgraph["neighbors"]:
            lines.append("【相邻实体】")
            for item in subgraph["neighbors"]:
                lines.append(f"- {item['name']}（{item.get('type') or '未分类'}）")
        if documents:
            lines.append("【原文片段】")
            for doc in documents:
                src = f"（来源：{doc['source_file']}）" if doc["source_file"] else ""
                lines.append(f"- {doc['content']}{src}")
        return "\n".join(lines)

    def _answer_with_llm(self, prompt: str, context: str) -> str:
        """让大模型基于图谱上下文回答；不可用时返回原始上下文。"""
        llm = self._get_llm()
        if llm is None:
            return context or "知识图谱中没有找到与该问题相关的内容。"

        nl = "\n"
        ask = (
            "你是知识图谱问答助手。请严格依据下面的知识图谱内容回答问题。"
            + nl
            + "如果图谱内容不足以回答，请直接说明“知识图谱中没有足够信息”，不要编造。"
            + nl
            + nl
            + "知识图谱内容："
            + nl
            + context
            + nl
            + nl
            + "问题："
            + prompt
            + nl
            + "回答："
        )
        answer = self._invoke_llm(ask).strip()
        return answer or context

    # =====================================================================
    # 辅助接口
    # =====================================================================
    def stats(self) -> Dict[str, int]:
        """返回图谱规模统计。"""
        with self._session() as session:
            record = session.run(
                f"CALL () {{ MATCH (e:{ENTITY_LABEL}) RETURN count(e) AS entities }} "
                f"CALL () {{ MATCH (d:{DOCUMENT_LABEL}) RETURN count(d) AS documents }} "
                f"CALL () {{ MATCH ()-[r]->() RETURN count(r) AS relations }} "
                f"RETURN entities, documents, relations"
            ).single()
        return dict(record) if record else {"entities": 0, "documents": 0, "relations": 0}

    def clear(self, confirm: bool = False) -> None:
        """清空本模块写入的节点与关系（需显式 ``confirm=True``）。"""
        if not confirm:
            raise ValueError("clear() 会删除全部知识图谱数据，请显式传入 confirm=True")
        with self._session() as session:
            session.run(
                f"MATCH (n) WHERE n:{ENTITY_LABEL} OR n:{DOCUMENT_LABEL} "
                f"DETACH DELETE n"
            ).consume()
        print("[KnowledgeGraphMemory] 已清空知识图谱")

    def close(self) -> None:
        """关闭数据库驱动。"""
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> "KnowledgeGraphMemory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = ["KnowledgeGraphMemory", "load_settings"]
